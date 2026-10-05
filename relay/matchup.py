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
"""
import csv
import json
import math
from datetime import date, datetime
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
class Data:
    def __init__(self, snap):
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


def availability(status):
    return AVAIL.get((status or "").upper(), 1.0)


def make_player(D, pid, slot=None):
    v = D.vals.get(str(pid))
    if not v:
        return None
    inj = v.get("injury_status") or ""
    return {"id": str(pid), "name": v["name"], "team": v["pro_team"],
            "pos": set((v.get("eligible") or "").split("/")) - {""},
            "eligible": v.get("eligible") or "", "line": player_line(v),
            "avail": availability(inj), "inj": inj if inj not in ("ACTIVE", "NORMAL") else "",
            "value": _num(v.get("value"), -99), "rank": int(_num(v.get("rank"), 999)),
            "slot": slot, "status": v.get("status")}


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
    """Greedy lineup: best players first, each into the most restrictive open slot.
    bench_negative: sit players whose matchup value is negative (they hurt)."""
    free = list(slots)
    starters, bench = [], []
    for p in sorted(players, key=key, reverse=True):
        if bench_negative and key(p) < 0:
            bench.append((p, "negative"))
            continue
        spot = next((s for s in free if p["pos"] & SLOT_ELIG.get(s, set())), None)
        if spot is None:
            bench.append((p, "no_slot"))
        else:
            free.remove(spot)
            starters.append((p, spot))
    return starters, bench, free


def plays(D, p, sp):
    return (sp, p["team"]) in D.games


def simulate(D, players, sps, key, bench_negative=False):
    """Expected remaining totals and variances for one team."""
    mean = {c: 0.0 for c in COMPONENTS}
    var = {c: 0.0 for c in COMPONENTS}
    games = 0.0
    for sp in sps:
        today = [p for p in players if p["slot"] != "IR" and p["avail"] > 0 and plays(D, p, sp)]
        starters, _, _ = assign(today, D.slots, key, bench_negative)
        for p, _slot in starters:
            a = p["avail"]
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
    return "safe" if p >= 0.8 else "favored" if p >= 0.6 else "toss-up" if p > 0.4 else "underdog" if p > 0.2 else "likely loss"


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


def outlook(D, mine, opp_sim, sps, key_m, cur_m, cur_o, bench_negative=False):
    rm, vm, gm = simulate(D, mine, sps, key_m, bench_negative)
    ro, vo, go = opp_sim
    return odds(D.cats, cur_m, cur_o, rm, vm, ro, vo), gm, go


# ---------------------------------------------------------------- main
def build(snap, today=None, n_fa=60, n_moves=5):
    D = Data(snap)
    today = today or today_ct()
    mp, sps, prow = current_period(D, today)
    if mp is None:
        return {"error": "no matchup period found", "date": today.isoformat()}
    opp = opponent(D, mp)
    mine = team_players(D, D.my_tid)
    theirs = team_players(D, opp) if opp else []
    cur_m, cur_o = current_totals(D, D.my_tid), current_totals(D, opp)
    in_progress = any(cur_m.values()) or any(cur_o.values())

    # opponent: starts its best players by generic value every day
    opp_sim = simulate(D, theirs, sps, lambda p: p["value"])
    # pass 1: generic ordering -> odds -> weights; pass 2: matchup ordering
    O, gm, go = outlook(D, mine, opp_sim, sps, lambda p: p["value"], cur_m, cur_o)
    key = lambda p: matchup_value(p, O)  # noqa: E731
    O, gm, go = outlook(D, mine, opp_sim, sps, key, cur_m, cur_o, True)
    key = lambda p: matchup_value(p, O)  # noqa: E731
    base_exp = sum(o["p"] for o in O.values())

    # today's lineup (first remaining day)
    sp0 = sps[0] if sps else None
    lineup = {"scoring_period": sp0, "date": D.sp_dates.get(sp0) if sp0 else None,
              "starters": [], "bench": [], "no_game": [], "ir": [], "open_slots": 0}
    if sp0:
        active = [p for p in mine if p["slot"] != "IR"]
        playing = [p for p in active if plays(D, p, sp0) and p["avail"] > 0]
        st, be, free = assign(playing, D.slots, key, True)
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
        lineup["ir"] = [_pinfo(p, O) for p in mine if p["slot"] == "IR"]

    # moves: add a free agent, drop a non-core player
    droppable = [p for p in mine if p["slot"] != "IR" and p["rank"] > 100]
    droppable.sort(key=lambda p: (p["value"]))
    drops = droppable[:3]
    ir_moves = [p for p in mine if p["slot"] != "IR" and p["avail"] == 0]
    fa = [make_player(D, pid) for pid, v in D.vals.items() if v.get("status") in ("FREEAGENT", "WAIVERS")]
    fa = [p for p in fa if p and p["avail"] > 0 and any(plays(D, p, sp) for sp in sps)]
    fa.sort(key=lambda p: -key(p) * sum(1 for sp in sps if plays(D, p, sp)))
    fa = fa[:n_fa]
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
            g_left = sum(1 for sp in sps if plays(D, x, sp))
            moves.append({"add": _pinfo(x, O, mv=key(x)), "drop": _pinfo(best[1], O, mv=key(best[1])),
                          "gain": round(best[0], 3), "games_left": g_left,
                          "plays_today": bool(sp0 and plays(D, x, sp0)),
                          "helps": helps(x, O)})
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

    cats = []
    for c in D.cats:
        o = O[c]
        cats.append({"cat": c, "mine": _r(o["mine"], c), "theirs": _r(o["theirs"], c),
                     "now_mine": _r(_now(cur_m, c), c), "now_theirs": _r(_now(cur_o, c), c),
                     "p": round(o["p"], 3), "label": label(o["p"]), "weight": round(o["w"], 5),
                     "att_mine": round(o["att_m"], 1) if o["att_m"] else None})

    return {
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
        "moves": chosen,
        "alternatives": alternatives,
        "ir_moves": [_pinfo(p, O) for p in ir_moves],
        "opp_injuries": [_pinfo(p, O) for p in theirs if p["inj"]],
    }


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
