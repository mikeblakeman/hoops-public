"""Matchup engine: this week's category outlook, today's lineup, streaming moves.

Reads the tidy snapshot files (so it can run inside the relay or offline) and
returns one JSON-able dict (written as snapshots/brief_data.json).

Model
- Each player has an expected per-game line (blend of projection, season and
  last 15) from valuations.csv, scaled by availability (injury status).
- Each remaining day of the matchup, each team starts its best eligible players
  into its lineup slots. Mike's lineup is ordered by matchup value; the
  opponent's by generic value.
- Category win odds: normal approximation on (current score + remaining
  projection), with per-game variance for counting stats, Bernoulli variance
  for double-doubles and binomial variance (inflated) for shooting makes.
- Matchup weights: marginal win probability per unit of each category. A
  player's matchup value is his stat line priced by those weights, so close
  categories matter and settled ones don't.
- Moves: "fills" add a free agent into an open roster spot (empty now, or
  opened by moving an out player to IR), picked greedily by expected
  categories gained. "moves" swap a free agent for a droppable player
  (ranked worse than 100).
"""
import csv
import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
STAT_ID = {"PTS": 0, "BLK": 1, "STL": 2, "AST": 3, "REB": 6, "TO": 11, "FGM": 13, "FGA": 14,
           "FTM": 15, "FTA": 16, "3PM": 17, "3PA": 18, "FG%": 19, "FT%": 20, "3PT%": 21, "DD": 37}
NAME_BY_ID = {v: k for k, v in STAT_ID.items()}
RATIO = {"FG%": ("FGM", "FGA"), "FT%": ("FTM", "FTA"), "3PT%": ("3PM", "3PA")}
COMPONENTS = ["PTS", "REB", "AST", "STL", "BLK", "3PM", "3PA", "FGM", "FGA", "FTM", "FTA", "DD", "TO"]
# per player-game coefficient of variation for counting stats
GAME_CV = {"PTS": 0.45, "REB": 0.45, "AST": 0.55, "STL": 0.9, "BLK": 1.0, "3PM": 0.75,
           "FGM": 0.45, "FTM": 0.7, "TO": 0.6}
SHOT_INFLATE = 1.3
AVAIL = {"OUT": 0.0, "SUSPENSION": 0.0, "INJURY_RESERVE": 0.0, "DAY_TO_DAY": 0.75,
         "QUESTIONABLE": 0.6, "DOUBTFUL": 0.25, "PROBABLE": 0.9}
SLOT_ELIG = {"PG": {"PG"}, "SG": {"SG"}, "SF": {"SF"}, "PF": {"PF"}, "C": {"C"},
             "G": {"PG", "SG"}, "F": {"SF", "PF"}, "UT": {"PG", "SG", "SF", "PF", "C"}}
SLOT_ORDER = ["PG", "SG", "SF", "PF", "C", "G", "F", "UT"]


def _rows(p):
    p = Path(p)
    if not p.exists() or p.stat().st_size == 0:
        return []
    with p.open() as f:
        return list(csv.DictReader(f))


def _num(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def phi(x):
    return math.exp(-x * x / 2) / math.sqrt(2 * math.pi)


def Phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


# ---------------------------------------------------------------- inputs
FREE = ("FREEAGENT", "WAIVERS")


def scores_by_stat(comp):
    """Component totals ({PTS, REB, ..., FGA, FTA, 3PA}) as ESPN's scoreByStat dict ({stat_id: score})."""
    out = {str(STAT_ID[k]): float(comp.get(k, 0.0)) for k in COMPONENTS if k in STAT_ID and k != "TO"}
    for r, (m, att) in RATIO.items():
        out[str(STAT_ID[r])] = (comp.get(m, 0.0) / comp[att]) if comp.get(att) else None
    return out


class Data:
    """Snapshot inputs. Strategy-lab hook (relay/lab.py): `my_roster` replaces my team's roster rows with a paper
    roster (list of {player_id, slot, ...}), `my_scores` replaces my team's matchup totals so far (component dict),
    and `free_ids` returns players the paper team dropped to its own free-agent pool. The engine itself is unchanged:
    a paper team runs through exactly the code the brief uses."""

    def __init__(self, snap, my_roster=None, my_scores=None, free_ids=()):
        snap = Path(snap)
        self.settings = json.loads((snap / "league_settings.json").read_text())
        self.status = json.loads((snap / "status.json").read_text())
        self.vals = {r["player_id"]: r for r in _rows(snap / "valuations.csv")}
        self.roster = _rows(snap / "rosters.csv")
        self.teams = {r["team_id"]: r for r in _rows(snap / "teams.csv")}
        self.sched = _rows(snap / "schedule.csv")
        self.cal = _rows(snap / "matchup_calendar.csv")
        self.sp_dates = {int(r["scoring_period"]): r["date_ct"] for r in _rows(snap / "scoring_period_dates.csv")}
        self.matchups = _rows(snap / "matchups.csv")
        inj = snap / "injuries.csv"
        self.ret = {r["player_id"]: r.get("return_date", "") for r in _rows(inj)} if inj.exists() else {}
        cs = snap / "current_scores.json"
        self.scores = json.loads(cs.read_text()) if cs.exists() else {}
        self.cats = [i["stat"] for i in self.settings["scoring_items"]]
        self.slots = []
        for s in SLOT_ORDER:
            self.slots += [s] * int(self.settings["lineup_slots"].get(s, 0))
        self.games = {}  # (sp, team) -> {opp, tip, home_away}
        for g in self.sched:
            self.games[(int(g["scoring_period"]), g["team"])] = g
        self.my_tid = str(self.status.get("my_team_id"))
        try:
            from . import calibrate
            self.calib = calibrate.load(snap)  # league-wide actual/projected correction, {} until earned
        except Exception:  # noqa: BLE001
            self.calib = {}
        if my_roster is not None:
            self._override_roster(my_roster, free_ids)
        if my_scores is not None:
            self.scores = dict(self.scores)
            self.scores[self.my_tid] = scores_by_stat(my_scores)

    def _override_roster(self, rows, free_ids=()):
        me = self.my_tid
        others = {r["player_id"] for r in self.roster if r["team_id"] != me}
        new = []
        for r in rows:
            pid = str(r["player_id"])
            v = self.vals.get(pid) or {}
            new.append({"team_id": me, "player_id": pid, "name": r.get("name") or v.get("name", ""),
                        "slot": r.get("slot") or "BE", "acquisition_type": r.get("acquisition_type") or "ADD",
                        "acquired_ct": r.get("acquired_ct") or "", "injury_status": v.get("injury_status", "")})
        self.roster = [r for r in self.roster if r["team_id"] != me] + new
        paper = {r["player_id"] for r in new}
        # a player the paper team dropped is back in ITS pool unless a real rival has rostered him since
        free = {str(i) for i in free_ids} - others - paper
        vals = {}
        for pid, v in self.vals.items():
            st = "ONTEAM" if pid in paper else "FREEAGENT" if pid in free else v.get("status")
            vals[pid] = v if st == v.get("status") else dict(v, status=st)
        self.vals = vals

    def with_my_roster(self, rows):
        """A copy of this Data with my team's roster replaced by `rows` (same shape as rosters.csv rows). Paper
        overrides already applied (scores, players the paper team dropped) carry over. Used to build the brief on
        the roster after an IR activation; a removed player keeps his snapshot status, so the engine never offers
        him straight back."""
        import copy
        D2 = copy.copy(self)
        D2._override_roster(rows)
        return D2


def player_line(v):
    """Expected per-game components from a valuations row."""
    fga, fta, tpa = _num(v.get("pg_FGA")), _num(v.get("pg_FTA")), _num(v.get("pg_3PA"))
    return {
        "PTS": _num(v.get("pg_PTS")), "REB": _num(v.get("pg_REB")), "AST": _num(v.get("pg_AST")),
        "STL": _num(v.get("pg_STL")), "BLK": _num(v.get("pg_BLK")), "3PM": _num(v.get("pg_3PM")),
        "TO": _num(v.get("pg_TO")), "FGA": fga, "FTA": fta, "3PA": tpa,
        "FGM": _num(v.get("pg_FGM")) or _num(v.get("pg_FG%")) * fga,
        "FTM": _num(v.get("pg_FTM")) or _num(v.get("pg_FT%")) * fta,
        "DD": _num(v.get("pg_DD")),
    }


def calibrated(D, line):
    f = getattr(D, "calib", None) or {}
    return {k: (x * f.get(k, 1.0)) for k, x in line.items()}


def availability(status):
    return AVAIL.get((status or "").upper(), 1.0)


HARD_OUT = {"OUT", "SUSPENSION", "INJURY_RESERVE"}


def avail_on(p, d):
    """Availability on ISO date d, using the injury report's return date when there is one."""
    st = (p.get("inj") or "").upper()
    if not st:
        return 1.0
    rd = p.get("ret") or ""
    if st in HARD_OUT:
        return 0.85 if rd and d >= rd else 0.0
    if rd and d > rd:
        return 1.0
    return AVAIL.get(st, 1.0)


def make_player(D, pid, slot=None):
    v = D.vals.get(str(pid))
    if not v:
        return None
    inj = v.get("injury_status") or ""
    return {"id": str(pid), "name": v["name"], "team": v["pro_team"],
            "pos": set((v.get("eligible") or "").split("/")) - {""},
            "eligible": v.get("eligible") or "", "line": calibrated(D, player_line(v)),
            "avail": availability(inj), "inj": inj if inj not in ("ACTIVE", "NORMAL") else "",
            "value": _num(v.get("value"), -99), "rank": int(_num(v.get("rank"), 999)),
            "slot": slot, "status": v.get("status"), "ret": getattr(D, "ret", {}).get(str(pid), "")}


def team_players(D, tid):
    out = []
    for r in D.roster:
        if r["team_id"] == str(tid):
            p = make_player(D, r["player_id"], r.get("slot"))
            if p:
                out.append(p)
    return out


# ---------------------------------------------------------------- calendar
def today_ct():
    return datetime.now(CT).date()


def current_period(D, today):
    """(matchup_period, list of remaining scoring periods incl. today, period row)."""
    t = today.isoformat()
    rows = sorted(D.cal, key=lambda r: int(r["matchup_period"]))
    for r in rows:
        if r["start"] <= t <= r["end"] or t < r["start"]:
            sps = [sp for sp in range(int(r["first_sp"]), int(r["last_sp"]) + 1)
                   if D.sp_dates.get(sp, "") >= t]
            return int(r["matchup_period"]), sps, r
    return None, [], None


def opponent(D, mp):
    for m in D.matchups:
        if int(_num(m["matchup_period"])) != mp:
            continue
        h, a = str(m["home_team_id"]), str(m["away_team_id"])
        if h == D.my_tid:
            return a
        if a == D.my_tid:
            return h
    return None


# ---------------------------------------------------------------- lineups
def assign(players, slots, key, bench_negative=False):
    """Best lineup: players are taken in priority order, and each is kept only if the
    whole group can still be seated (bipartite matching with re-seating), so a
    multi-position player never blocks someone else from a slot.
    bench_negative: sit players whose matchup value is negative (they hurt)."""
    slots = list(slots)
    seat = {}  # slot index -> player index
    cand = []
    bench = []
    for p in sorted(players, key=key, reverse=True):
        if bench_negative and key(p) < 0:
            bench.append((p, "negative"))
        else:
            cand.append(p)

    def fits(pi, i):
        return bool(cand[pi]["pos"] & SLOT_ELIG.get(slots[i], set()))

    def place(pi, seen):
        for i in range(len(slots)):
            if i in seen or not fits(pi, i):
                continue
            seen.add(i)
            if i not in seat or place(seat[i], seen):
                seat[i] = pi
                return True
        return False

    kept = set()
    for pi in range(len(cand)):
        if place(pi, set()):
            kept.add(pi)
    for pi in range(len(cand)):
        if pi not in kept:
            bench.append((cand[pi], "no_slot"))
    starters = [(cand[pi], slots[i]) for i, pi in sorted(seat.items(), key=lambda kv: key(cand[kv[1]]), reverse=True)]
    free = [slots[i] for i in range(len(slots)) if i not in seat]
    return starters, bench, free


def plays(D, p, sp):
    return (sp, p["team"]) in D.games


def simulate(D, players, sps, key, bench_negative=False):
    """Expected remaining totals and variances for one team."""
    mean = {c: 0.0 for c in COMPONENTS}
    var = {c: 0.0 for c in COMPONENTS}
    games = 0.0
    for sp in sps:
        d = D.sp_dates.get(sp, "")
        today = [p for p in players if p["slot"] != "IR" and avail_on(p, d) > 0 and plays(D, p, sp)]
        starters, _, _ = assign(today, D.slots, key, bench_negative)
        for p, _slot in starters:
            a = avail_on(p, d)
            games += a
            for c in COMPONENTS:
                mean[c] += a * p["line"][c]
            for c, cv in GAME_CV.items():
                var[c] += a * (cv * p["line"][c]) ** 2
            dd = p["line"]["DD"]
            var["DD"] += a * dd * (1 - dd)
            for r, (m, att) in RATIO.items():
                pct = p["line"][m] / p["line"][att] if p["line"][att] else 0
                var[m + "|shot"] = var.get(m + "|shot", 0.0) + a * (SHOT_INFLATE ** 2) * p["line"][att] * pct * (1 - pct)
    return mean, var, games


def current_totals(D, tid):
    sc = D.scores.get(str(tid)) or {}
    cur = {c: 0.0 for c in COMPONENTS}
    for sid, val in sc.items():
        name = NAME_BY_ID.get(int(sid))
        if name and name in cur and val is not None:
            cur[name] = float(val)
    for r, (m, att) in RATIO.items():
        pct = sc.get(str(STAT_ID[r]))
        if pct and cur[m]:
            cur[att] = cur[m] / float(pct)
    return cur


def odds(cats, cur_m, cur_o, rm, vm, ro, vo):
    out = {}
    for c in cats:
        if c in RATIO:
            m, att = RATIO[c]
            Mm, Am = cur_m[m] + rm[m], cur_m[att] + rm[att]
            Mo, Ao = cur_o[m] + ro[m], cur_o[att] + ro[att]
            pm = Mm / Am if Am else 0.0
            po = Mo / Ao if Ao else 0.0
            sd = math.sqrt((vm.get(m + "|shot", 0) / Am ** 2 if Am else 0) + (vo.get(m + "|shot", 0) / Ao ** 2 if Ao else 0))
            diff = pm - po
            mine, theirs = pm, po
        else:
            mine, theirs = cur_m[c] + rm[c], cur_o[c] + ro[c]
            diff = mine - theirs
            sd = math.sqrt(vm.get(c, 0) + vo.get(c, 0))
        if sd < 1e-9:
            p = 1.0 if diff > 0 else (0.5 if diff == 0 else 0.0)
            w = 0.0
        else:
            p = Phi(diff / sd)
            w = phi(diff / sd) / sd
        out[c] = {"mine": mine, "theirs": theirs, "p": p, "w": w,
                  "att_m": (cur_m[RATIO[c][1]] + rm[RATIO[c][1]]) if c in RATIO else None}
    return out


def label(p):
    """Category status shown on the page. Cutoffs (Mike, 2026-10-24): only 85%+ is safe and only 15% or
    less is a likely loss, so a category like DD at 20% with days left stays in play."""
    return "safe" if p >= 0.85 else "favored" if p >= 0.6 else "toss-up" if p > 0.4 else "underdog" if p > 0.15 else "likely loss"


def matchup_value(p, O):
    """Expected category wins added per game by this player, given current odds O."""
    v = 0.0
    for c, o in O.items():
        if c in RATIO:
            m, att = RATIO[c]
            if o["att_m"]:
                v += o["w"] * (p["line"][m] - o["mine"] * p["line"][att]) / o["att_m"]
        else:
            v += o["w"] * p["line"].get(c, 0.0)
    return v * p["avail"]


def helps(p, O, top=3):
    parts = []
    for c, o in O.items():
        if c in RATIO:
            m, att = RATIO[c]
            x = o["w"] * (p["line"][m] - o["mine"] * p["line"][att]) / o["att_m"] if o["att_m"] else 0
        else:
            x = o["w"] * p["line"].get(c, 0.0)
        parts.append((x, c))
    parts.sort(reverse=True)
    return [c for x, c in parts[:top] if x > 0.002]


def roster_room(D, ir_moves):
    """(empty active roster spots now, ir_moves that fit in open IR slots).
    Active capacity is every non-IR lineup slot, bench included."""
    ls = D.settings.get("lineup_slots") or {}
    cap_active = sum(int(v) for k, v in ls.items() if k != "IR")
    cap_ir = int(ls.get("IR", 0))
    rows = [r for r in D.roster if r["team_id"] == D.my_tid]
    n_ir = sum(1 for r in rows if r.get("slot") == "IR")
    empty = max(0, cap_active - (len(rows) - n_ir))
    return empty, ir_moves[:max(0, cap_ir - n_ir)]


def outlook(D, mine, opp_sim, sps, key_m, cur_m, cur_o, bench_negative=False):
    rm, vm, gm = simulate(D, mine, sps, key_m, bench_negative)
    ro, vo, go = opp_sim
    return odds(D.cats, cur_m, cur_o, rm, vm, ro, vo), gm, go


# ---------------------------------------------------------------- IR activation
# ESPN (support.espn.com, read 2026-11-11): only players tagged Out, IR or IL may sit in an IR slot, and while a
# healthy player sits there ESPN refuses EVERY add. Activating him needs a free active roster spot.
# claude/engine/ir-activation-spec.md
IR_OK = {"OUT", "INJURY_RESERVE"}
SHORT_STASH_DAYS = 2   # an IR move whose return date is this close says the add into his spot is temporary


def ir_returns(D, day, sps=None):
    """My IR-slot players who must come off IR. {"now": [player], "returning": [(player, iso date)]}.
    now: not IR-eligible on `day` (valuations status not OUT or INJURY_RESERVE, or the injury report's return date
    is on or before `day`). returning: still eligible today, but the return date falls on a later remaining day of
    this matchup (`sps`); the date is the first of those days on or after the return date.
    rosters.csv injury_status is NORMAL for every row, so it is never used; make_player reads valuations.csv."""
    d = day.isoformat() if hasattr(day, "isoformat") else str(day)
    later = sorted(D.sp_dates[sp] for sp in (sps or []) if D.sp_dates.get(sp, "") > d)
    now, returning = [], []
    for r in D.roster:
        if r["team_id"] != D.my_tid or r.get("slot") != "IR":
            continue
        p = make_player(D, r["player_id"], "IR")
        if not p:
            continue
        st = ((D.vals.get(p["id"]) or {}).get("injury_status") or "").upper()
        rd = p.get("ret") or ""
        if st not in IR_OK or (rd and rd <= d):
            now.append(p)
        elif rd and later and rd <= later[-1]:
            returning.append((p, next(x for x in later if x >= rd)))
    return {"now": now, "returning": returning}


def protected_ids(snap, D):
    """Keepers plus my top 10 by season value: never dropped (the strategy lab's helper, relay.strategies)."""
    from . import strategies as S   # lazy: strategies imports this module
    return set(S.protected_set(snap, D, S.real_rows(D)))


def _act_rows(D, activate_ids=(), drop_ids=()):
    act, drops = {str(i) for i in activate_ids}, {str(i) for i in drop_ids}
    out = []
    for r in D.roster:
        if r["team_id"] != D.my_tid or r["player_id"] in drops:
            continue
        # keep acquisition_type and acquired_ct: a drafted player coming off IR is never the streaming spot
        out.append(dict(r, slot="BE") if r["player_id"] in act else dict(r))
    return out


def post_activation(D, activate_ids=(), drop_ids=()):
    """Data on the roster after moving `activate_ids` off IR (to the bench) and dropping `drop_ids`."""
    if not activate_ids and not drop_ids:
        return D
    return D.with_my_roster(_act_rows(D, activate_ids, drop_ids))


def _game_words(g):
    if not g:
        return None
    h, m = (int(x) for x in g["tip_ct"].split(":")[:2])
    tip = f"{(h - 1) % 12 + 1}:{m:02d} {'AM' if h < 12 else 'PM'}"
    return f"{'vs' if g.get('home_away') == 'home' else 'at'} {g['opp']} at {tip}"


def choose_drop(D, roster, player, ctx, protected, empty=0):
    """Best way to bring `player` (on IR in `roster`) back to the active roster over the days in ctx["sps"].
    Scores every active non-IR player outside `protected` by expected categories with `player` active and that
    player dropped (the same outlook as fills and moves) and picks the best. An empty active spot means no drop.
    Every candidate protected: blocked, with the best two options for Mike to choose from."""
    sps, key, opp_sim = ctx["sps"], ctx["key"], ctx["opp_sim"]
    cur_m, cur_o = ctx["cur_m"], ctx["cur_o"]

    def exp(r):
        O2, _, _ = outlook(D, r, opp_sim, sps, key, cur_m, cur_o, True)
        return sum(o["p"] for o in O2.values())

    base = exp(roster)
    d0 = D.sp_dates.get(sps[0], "") if sps else ""
    # a player due back is ranked by his availability on the return day (his flat status may still say OUT)
    on = [dict(p, slot="BE", avail=max(p["avail"], avail_on(p, d0))) if p["id"] == player["id"] else p
          for p in roster]
    if empty > 0:
        return {"drop": None, "gain": exp(on) - base, "blocked": False, "options": []}
    scored = sorted(((exp([q for q in on if q is not x]) - base, x) for x in on
                     if x["slot"] != "IR" and x["id"] != player["id"]), key=lambda t: -t[0])
    free = [(g, x) for g, x in scored if str(x["id"]) not in protected]
    if free:
        return {"drop": free[0][1], "gain": free[0][0], "blocked": False, "options": []}
    return {"drop": None, "gain": None, "blocked": True, "options": scored[:2]}


def plan_activations(D, snap, today, ctx=None, protected=None):
    """Healthy players stuck on IR today, each with the drop that frees his spot. Returns (activations, D after,
    ctx after). Activations are applied in order on the roster left by the ones before. A blocked one (every
    possible drop is protected) changes nothing and blocks adds until Mike chooses."""
    ctx = ctx or _context(D, today)
    if not ctx or not ctx["sps"]:
        return [], D, ctx
    need = ir_returns(D, today, ctx["sps"])["now"]
    if not need:
        return [], D, ctx
    protected = {str(i) for i in (protected if protected is not None else protected_ids(snap, D))}
    roster, O, sp0 = list(ctx["mine"]), ctx["O"], ctx["sps"][0]
    empty = roster_room(D, [])[0]
    acts, act_ids, drop_ids = [], [], []
    for p in need:
        ch = choose_drop(D, roster, p, ctx, protected, empty)
        g = D.games.get((sp0, p["team"]))
        a = {"player": _pinfo(p, O, game=g), "drop": _pinfo(ch["drop"], O) if ch["drop"] else None,
             "gain": None if ch["gain"] is None else round(ch["gain"], 3), "plays_today": bool(g),
             "tip": g["tip_ct"] if g else None, "game": _game_words(g), "blocked": ch["blocked"],
             "options": [dict(_pinfo(x, O), gain=round(gn, 3)) for gn, x in ch["options"]],
             "status": ((D.vals.get(p["id"]) or {}).get("injury_status") or "").upper(),
             "return_date": p.get("ret") or ""}
        a["reason"] = ("ESPN shows him " + (a["status"].replace("_", " ").lower() or "active") +
                       (f" (return date {a['return_date']})" if a["return_date"] else "") +
                       ", so he cannot stay on IR, and ESPN blocks every add until he leaves it.")
        acts.append(a)
        if ch["blocked"]:
            continue
        roster = [dict(q, slot="BE") if q["id"] == p["id"] else q for q in roster if q is not ch["drop"]]
        act_ids.append(p["id"])
        if ch["drop"]:
            drop_ids.append(ch["drop"]["id"])
        else:
            empty -= 1
    if not act_ids:
        return acts, D, ctx
    D2 = post_activation(D, act_ids, drop_ids)
    return acts, D2, _context(D2, today)


def returns_later(D, snap, today, sps, stream_days=None, protected=None, players=None, add_ids=()):
    """IR players due back on a later day of this matchup, each with the drop to make that morning, picked on that
    day's roster: today's open-spot adds (`add_ids`) kept, the players going to IR today on IR, and the streaming
    spot held by the plan's player that day. `players`: extra [(player, date)] for short IR stashes recommended
    today. Returns [{player, date, drop, gain, blocked, options}]."""
    found = ir_returns(D, today, sps)["returning"] + list(players or [])
    if not found:
        return []
    protected = {str(i) for i in (protected if protected is not None else protected_ids(snap, D))}
    occ = {d["date"]: d.get("id") for d in (stream_days or [])}
    occ_now = None
    if stream_days:
        d0 = stream_days[0]
        occ_now = d0.get("drop_id") if d0.get("swap") else d0.get("id")
    out = []
    for p, when in found:
        c = _context(D, date.fromisoformat(when))
        if not c or not c["sps"]:
            continue
        stash = {str(q["id"]) for q, _w in (players or [])}
        roster = [dict(q, slot="IR") if str(q["id"]) in stash else q for q in c["mine"]]
        for pid in add_ids:
            x = make_player(D, pid)
            if x and not any(q["id"] == x["id"] for q in roster):
                roster.append(x)
        holder = occ.get(when)
        if occ_now and holder and holder != occ_now:
            roster = [q for q in roster if q["id"] != occ_now]
            x = make_player(D, holder)
            if x and not any(q["id"] == holder for q in roster):
                roster.append(x)
        cap_active = sum(int(v) for k, v in (D.settings.get("lineup_slots") or {}).items() if k != "IR")
        empty = max(0, cap_active - sum(1 for q in roster if q["slot"] != "IR"))
        ch = choose_drop(D, roster, p, c, protected, empty)
        out.append({"player": _pinfo(p, c["O"]), "date": when, "drop": _pinfo(ch["drop"], c["O"]) if ch["drop"] else None,
                    "gain": None if ch["gain"] is None else round(ch["gain"], 3), "blocked": ch["blocked"],
                    "options": [dict(_pinfo(x, c["O"]), gain=round(g, 3)) for g, x in ch["options"]]})
    return out


# ---------------------------------------------------------------- main
def _context(D, today):
    """Everything the lineup and move search share: period, both rosters, scores, the opponent's simulated
    week and the matchup-weighted ordering key. None if no matchup period covers `today`."""
    mp, sps, prow = current_period(D, today)
    if mp is None:
        return None
    opp = opponent(D, mp)
    mine = team_players(D, D.my_tid)
    theirs = team_players(D, opp) if opp else []
    cur_m, cur_o = current_totals(D, D.my_tid), current_totals(D, opp)
    # opponent: starts its best players by generic value every day
    opp_sim = simulate(D, theirs, sps, lambda p: p["value"])
    # pass 1: generic ordering -> odds -> weights; pass 2: matchup ordering
    O, gm, go = outlook(D, mine, opp_sim, sps, lambda p: p["value"], cur_m, cur_o)
    key = lambda p: matchup_value(p, O)  # noqa: E731
    O, gm, go = outlook(D, mine, opp_sim, sps, key, cur_m, cur_o, True)
    key = lambda p: matchup_value(p, O)  # noqa: E731
    return dict(mp=mp, sps=sps, prow=prow, opp=opp, mine=mine, theirs=theirs, cur_m=cur_m, cur_o=cur_o,
                opp_sim=opp_sim, O=O, gm=gm, go=go, key=key)


def _lineup(D, roster, sp0, key, O, bench_ids=()):
    """Best lineup for one day from `roster` (IR players stay on IR). bench_ids: players a strategy sits today
    (shown on the bench as hurting close categories)."""
    lineup = {"scoring_period": sp0, "date": D.sp_dates.get(sp0) if sp0 else None,
              "starters": [], "bench": [], "no_game": [], "ir": [], "open_slots": 0}
    if not sp0:
        return lineup
    active = [p for p in roster if p["slot"] != "IR"]
    sit = {str(i) for i in bench_ids}
    playing = [p for p in active if plays(D, p, sp0) and p["avail"] > 0]
    st, be, free = assign([p for p in playing if str(p["id"]) not in sit], D.slots, key, True)
    be = be + [(p, "negative") for p in playing if str(p["id"]) in sit]
    lineup["open_slots"] = len(free)
    for p, slot in st:
        g = D.games[(sp0, p["team"])]
        lineup["starters"].append(_pinfo(p, O, slot=slot, game=g, mv=key(p)))
    for p, why in be:
        g = D.games[(sp0, p["team"])]
        lineup["bench"].append(_pinfo(p, O, game=g, mv=key(p), why=why))
    for p in active:
        if p not in playing:
            why = "out" if p["avail"] == 0 else "no game"
            lineup["no_game"].append(_pinfo(p, O, why=why))
    lineup["ir"] = [_pinfo(p, O) for p in roster if p["slot"] == "IR"]
    return lineup


def _roster_after(D, mine, add_ids=(), drop_ids=(), ir_ids=()):
    drop_ids, ir_ids = {str(i) for i in drop_ids}, {str(i) for i in ir_ids}
    roster = []
    for p in mine:
        if str(p["id"]) in drop_ids:
            continue
        if str(p["id"]) in ir_ids:
            p = dict(p, slot="IR")
        roster.append(p)
    have = {str(p["id"]) for p in roster}
    for pid in add_ids:
        x = make_player(D, str(pid))
        if x and str(x["id"]) not in have:
            roster.append(x)
            have.add(str(x["id"]))
    return roster


def lineup_after(snap, today, add_ids=(), drop_ids=(), ir_ids=(), bench_ids=(), activate_ids=(),
                 activate_drop_ids=()):
    """Today's lineup after the moves the brief recommends (players moved off IR, added, dropped or moved to IR),
    so the page's lineup card shows the same players as its actions. None if it cannot be built.
    activate_ids / activate_drop_ids: today's IR activations and the drops that make room for them (applied first,
    the same post-activation roster build() uses)."""
    D = post_activation(Data(snap), activate_ids, activate_drop_ids)
    ctx = _context(D, today)
    if not ctx or not ctx["sps"]:
        return None
    roster = _roster_after(D, ctx["mine"], add_ids, drop_ids, ir_ids)
    return _lineup(D, roster, ctx["sps"][0], ctx["key"], ctx["O"], bench_ids)


def coverage_after(snap, today, add_ids=(), drop_ids=(), ir_ids=(), current_streamer=None, streamer_by_date=None,
                   activate_ids=(), activate_drop_ids=(), returns=()):
    """Day-by-day slot coverage for the rest of the matchup on the roster the brief recommends: today's IR
    activations, IR moves and open-spot adds kept all week, and the streaming spot held by the plan's player for
    each date (`streamer_by_date`: date -> player id or None, replacing `current_streamer`). `returns`: IR players
    due back later this matchup, [{id, date, drop_id}]; from that date he is active and drop_id is gone. Today's
    row uses the same availability rule as the lineup card, so the two always agree. Returns [{date, my_starters,
    my_open_slots, my_no_slot, starters}] or None."""
    D = post_activation(Data(snap), activate_ids, activate_drop_ids)
    ctx = _context(D, today)
    if not ctx or not ctx["sps"]:
        return None
    base = _roster_after(D, ctx["mine"], add_ids, drop_ids, ir_ids)
    cur = str(current_streamer) if current_streamer else None
    by_date = {k: (str(v) if v else None) for k, v in (streamer_by_date or {}).items()}
    key, out = ctx["key"], []
    for i, sp in enumerate(ctx["sps"]):
        d = D.sp_dates[sp]
        roster = base
        if d in by_date:
            occ = by_date[d]
            roster = [p for p in base if str(p["id"]) != cur]
            if occ and occ not in {str(p["id"]) for p in roster}:
                x = make_player(D, occ)
                if x:
                    roster.append(x)
        for r in returns or ():
            if d >= r["date"]:
                gone = str(r.get("drop_id") or "")
                roster = [dict(p, slot="BE", avail=max(p["avail"], avail_on(p, d))) if str(p["id"]) == str(r["id"])
                          else p for p in roster if str(p["id"]) != gone]
        active = [p for p in roster if p["slot"] != "IR"]
        if i == 0:
            playing = [p for p in active if plays(D, p, sp) and p["avail"] > 0]
        else:
            playing = [p for p in active if plays(D, p, sp) and avail_on(p, d) > 0]
        st, be, free = assign(playing, D.slots, key, True)
        out.append({"date": d, "my_starters": len(st), "my_open_slots": free,
                    "my_no_slot": [p["name"] for p, w in be if w == "no_slot"],
                    "starters": [{"name": p["name"], "slot": s} for p, s in st]})
    return out


def build(snap, today=None, n_fa=60, n_moves=5, max_fills=4, D=None, with_trades=True, protected=None):
    """D: a prebuilt Data (the strategy lab passes one with a paper roster). with_trades=False skips the
    league-wide trade search, which changes nothing in the lineup, moves or stream plan. protected: player ids an
    IR activation may never drop (default: keepers plus the top 10, relay.strategies.protected_set).

    A healthy player in an IR slot blocks every add in ESPN, so he comes off IR first (plan_activations) and
    everything below (lineup, fills, moves, week plan, next opponent, trades) is built on the roster after it."""
    D = D or Data(snap)
    today = today or today_ct()
    ctx = _context(D, today)
    if ctx is None:
        return {"error": "no matchup period found", "date": today.isoformat()}
    if protected is None and ir_returns(D, today, ctx["sps"])["now"]:
        protected = protected_ids(snap, D)
    acts, D, ctx = plan_activations(D, snap, today, ctx, protected)
    adds_blocked = any(a["blocked"] for a in acts)
    mp, sps, prow, opp = ctx["mp"], ctx["sps"], ctx["prow"], ctx["opp"]
    mine, theirs, cur_m, cur_o = ctx["mine"], ctx["theirs"], ctx["cur_m"], ctx["cur_o"]
    opp_sim, O, gm, go, key = ctx["opp_sim"], ctx["O"], ctx["gm"], ctx["go"], ctx["key"]
    in_progress = any(cur_m.values()) or any(cur_o.values())
    base_exp = sum(o["p"] for o in O.values())

    # today's lineup (first remaining day)
    sp0 = sps[0] if sps else None
    lineup = _lineup(D, mine, sp0, key, O)

    # moves: add a free agent, drop a non-core player
    droppable = [p for p in mine if p["slot"] != "IR" and p["rank"] > 100]
    droppable.sort(key=lambda p: (p["value"]))
    drops = droppable[:3]
    ir_moves = [p for p in mine if p["slot"] != "IR" and p["avail"] == 0]
    fa = [make_player(D, pid) for pid, v in D.vals.items() if v.get("status") in ("FREEAGENT", "WAIVERS")]
    fa = [p for p in fa if p and p["avail"] > 0 and any(plays(D, p, sp) for sp in sps)]
    fa.sort(key=lambda p: -key(p) * sum(1 for sp in sps if plays(D, p, sp)))
    fa = fa[:n_fa]

    def games_left(x):
        return sum(1 for sp in sps if plays(D, x, sp))

    def move_info(x, gain, drop=None):
        m = {"add": _pinfo(x, O, mv=key(x)), "gain": round(gain, 3), "games_left": games_left(x),
             "plays_today": bool(sp0 and plays(D, x, sp0)), "helps": helps(x, O)}
        if drop is not None:
            m["drop"] = _pinfo(drop, O, mv=key(drop))
        return m

    # fills: open roster spots (empty now, then any opened by IR moves), one
    # greedy pick at a time so each add accounts for the ones before it
    empty, ir_room = roster_room(D, ir_moves)
    spots = ([None] * empty + list(ir_room))[:max_fills]
    fills, fill_alts = [], []
    roster_f, base_f, pool = list(mine), base_exp, list(fa)
    for via_ir in spots:
        scored = []
        for x in pool:
            O2, _, _ = outlook(D, roster_f + [x], opp_sim, sps, key, cur_m, cur_o, True)
            scored.append((sum(o["p"] for o in O2.values()) - base_f, x))
        scored.sort(key=lambda t: -t[0])
        if not scored or scored[0][0] <= 0.005:
            break
        gain, x = scored[0]
        f = move_info(x, gain)
        f["after_ir_move"] = via_ir["name"] if via_ir else None
        fills.append(f)
        if not fill_alts:
            fill_alts = [move_info(y, g) for g, y in scored[1:1 + n_moves] if g > 0.005]
        roster_f.append(x)
        base_f += gain
        pool.remove(x)
    if adds_blocked:          # ESPN refuses every add until the healthy IR player is activated
        fills, fill_alts = [], []
    fill_ids = {f["add"]["id"] for f in fills}
    fa = [x for x in fa if x["id"] not in fill_ids]

    moves = []
    for x in fa:
        best = None
        for d in drops:
            roster2 = [p for p in mine if p is not d] + [x]
            O2, _, _ = outlook(D, roster2, opp_sim, sps, key, cur_m, cur_o, True)
            gain = sum(o["p"] for o in O2.values()) - base_exp
            if best is None or gain > best[0]:
                best = (gain, d)
        if best and best[0] > 0.01:
            moves.append(move_info(x, best[0], drop=best[1]))
    moves.sort(key=lambda m: -m["gain"])
    # keep one move per drop candidate at the top, then the rest as alternatives
    chosen, used = [], set()
    for m in moves:
        if m["drop"]["id"] not in used:
            chosen.append(m)
            used.add(m["drop"]["id"])
        if len(chosen) >= len(drops):
            break
    alternatives = [m for m in moves if m not in chosen][:n_moves]
    if adds_blocked:
        chosen, alternatives = [], []

    cats = []
    for c in D.cats:
        o = O[c]
        cats.append({"cat": c, "mine": _r(o["mine"], c), "theirs": _r(o["theirs"], c),
                     "now_mine": _r(_now(cur_m, c), c), "now_theirs": _r(_now(cur_o, c), c),
                     "p": round(o["p"], 3), "label": label(o["p"]), "weight": round(o["w"], 5),
                     "att_mine": round(o["att_m"], 1) if o["att_m"] else None})

    result = {
        "generated_ct": datetime.now(CT).isoformat(timespec="minutes"),
        "data_run_ct": D.status.get("run_ct"),
        "date": today.isoformat(),
        "matchup_period": mp,
        "period": {"start": prow["start"], "end": prow["end"], "source": prow.get("source"),
                   "days_left": len(sps), "playoffs": prow.get("playoffs") in ("True", True)},
        "in_progress": in_progress,
        "me": {"team_id": D.my_tid, "name": D.teams.get(D.my_tid, {}).get("name")},
        "opponent": {"team_id": opp, "name": D.teams.get(str(opp), {}).get("name"),
                     "owners": D.teams.get(str(opp), {}).get("owners")},
        "expected_cats": round(base_exp, 2),
        "player_games_left": {"mine": round(gm, 1), "theirs": round(go, 1)},
        "categories": cats,
        "lineup": lineup,
        "roster_room": {"empty": empty, "via_ir": [p["name"] for p in ir_room]},
        "fills": fills,
        "fill_alternatives": fill_alts,
        "moves": chosen,
        "alternatives": alternatives,
        "ir_moves": [_pinfo(p, O) for p in ir_moves],
        "opp_injuries": [_pinfo(p, O) for p in theirs if p["inj"]],
        "ir_activations": acts,
        "adds_blocked": adds_blocked,
    }
    # day-by-day plan for the whole matchup (coverage, streaming spot, trades); advisory,
    # so a failure here must never cost the daily brief
    try:
        from . import weekplan
        result["week_plan"] = weekplan.plan(snap, today, D, with_trades=with_trades)
    except Exception as e:  # noqa: BLE001
        result["week_plan"] = {"error": f"{type(e).__name__}: {e}"}
    # IR players due back later this matchup (and short IR stashes made today): the drop to make that morning
    try:
        wp = result["week_plan"]
        days = ((wp.get("stream_plan") or {}).get("plans") or {}).get("daily", {}).get("days") \
            if not wp.get("error") else None
        short = []
        end = D.sp_dates.get(sps[-1], "") if sps else ""
        soon = (today + timedelta(days=SHORT_STASH_DAYS)).isoformat()
        moved = {p["id"] for p in ir_room}
        for p in ir_moves:
            rd = p.get("ret") or ""
            if p["id"] in moved and rd and rd <= soon and rd <= end:
                later = sorted(D.sp_dates[sp] for sp in sps if D.sp_dates[sp] >= max(rd, today.isoformat())
                               and D.sp_dates[sp] > today.isoformat())
                if later:
                    short.append((p, later[0]))
        rl = returns_later(D, snap, today, sps, days, protected, players=short,
                           add_ids=[f["add"]["id"] for f in fills])
        short_ids = {p["id"] for p, _w in short}
        result["ir_returns_later"] = [r for r in rl if r["player"]["id"] not in short_ids]
        by = {r["player"]["id"]: r for r in rl}
        for m in result["ir_moves"]:
            r = by.get(m["id"])
            if r and m["id"] in short_ids:
                m["return"] = {"date": r["date"], "drop": r["drop"], "blocked": r["blocked"]}
    except Exception as e:  # noqa: BLE001  advisory; never lose the daily brief
        result["ir_returns_later"] = []
        result["ir_returns_error"] = f"{type(e).__name__}: {e}"
    return result


def _now(cur, c):
    if c in RATIO:
        m, att = RATIO[c]
        return cur[m] / cur[att] if cur[att] else None
    return cur.get(c)


def _r(x, c):
    if x is None:
        return None
    return round(x, 3) if c in RATIO else round(x, 1)


def _pinfo(p, O, slot=None, game=None, mv=None, why=None):
    d = {"id": p["id"], "name": p["name"], "team": p["team"], "pos": p["eligible"], "inj": p["inj"],
         "rank": p["rank"]}
    if slot:
        d["slot"] = slot
    if game:
        d["opp"] = game["opp"]
        d["tip"] = game["tip_ct"]
        d["home_away"] = game["home_away"]
    if mv is not None:
        d["matchup_value"] = round(mv, 4)
    if why:
        d["why"] = why
    line = p["line"]
    d["line"] = {k: round(line[k], 2) for k in ("PTS", "REB", "AST", "STL", "BLK", "3PM", "DD")}
    d["helps"] = helps(p, O)
    return d


if __name__ == "__main__":
    import sys
    snap = sys.argv[1] if len(sys.argv) > 1 else "snapshots"
    day = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else None
    print(json.dumps(build(snap, day), indent=2))
