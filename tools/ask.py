"""Quick answers about today's games and lineup, sized for a light model in a chat.

Usage (from any directory; `-I` keeps the mirror's files from shadowing Python modules):
  python3 -I ~/hoops/tools/ask.py ~/hoops/snapshots <command> [args] [--teams ~/teams.json]

Commands
  today            the brief's moves for today, the lineup, open slots and this week's category odds
  games            NBA games on the day with tip times, plus my players and my opponent's in each
  fa               best free agents to add, ranked by expected categories gained this week
  alt NAME         NAME was taken, is out, or is the player to replace: the best replacements
  player NAME      owner, injury, games left this week, per-game line, categories he helps

Options (any command)
  --date D         today (default), tomorrow, a weekday (fri), 11/6 or 2026-11-06
  --taken NAME     rostered by another team since the data run (repeat for each)
  --added NAME     Mike added him since the data run (repeat)
  --dropped NAME   Mike dropped him since the data run (repeat)
  --for NAME       (fa, alt) compare against dropping this player instead of the streaming spot's player
  --pos P          (fa) only players eligible at P (PG, SG, SF, PF, C, G, F)
  --week           (fa) include players with no game on the day but games later this week
  -n N             how many players to list (default 6)
  --teams FILE     team names by id (project doc claude/league/teams.json)
  --pending FILE   trades still in ESPN review (project doc claude/league/pending-trades.json)

"cats" = change in expected categories won this week (out of 12) if he is added now and held through the
matchup's last day. 0.10 is a real gain; under 0.03 is noise. Free-agent status is as of the last data run.
Read-only: never writes files outside a temp folder and never logs decisions.
"""
import argparse
import json
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
_here = Path(__file__).resolve().parents[1]
for _root in (_here, Path.home() / "hoops"):
    if (_root / "relay" / "matchup.py").exists():
        for _p in (str(_root / "tools"), str(_root)):
            if _p not in sys.path:
                sys.path.insert(0, _p)
        break
from relay import matchup as MU  # noqa: E402
from relay import weekplan as WP  # noqa: E402

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
SUFFIX = {"jr", "sr", "ii", "iii", "iv", "v"}
FA = ("FREEAGENT", "WAIVERS")


# ------------------------------------------------------------------ small helpers
def now_ct():
    return datetime.now(CT)


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9 ]", "", s.replace("-", " ").replace(".", " "))
    return " ".join(w for w in s.split() if w not in SUFFIX)


def initials(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return "".join(w[0] for w in re.sub(r"[^a-z ]", " ", s.replace("'", "")).split())


def day_label(iso):
    d = date.fromisoformat(iso)
    return f"{d.strftime('%a')} {d.month}/{d.day}"


def tip_label(tip):
    if not tip:
        return "time TBD"
    h, m = (int(x) for x in tip.split(":")[:2])
    return f"{(h - 1) % 12 + 1}:{m:02d} {'AM' if h < 12 else 'PM'}"


def tipped(iso, tip, now):
    if not tip or iso != now.date().isoformat():
        return iso < now.date().isoformat()
    h, m = (int(x) for x in tip.split(":")[:2])
    return now >= datetime.fromisoformat(iso).replace(hour=h, minute=m, tzinfo=CT)


def parse_day(s, now):
    t = now.date()
    s = (s or "today").strip().lower()
    if s == "today":
        return t
    if s == "tomorrow":
        return t + timedelta(days=1)
    if s[:3] in DAYS:
        return t + timedelta(days=(DAYS.index(s[:3]) - t.weekday()) % 7)
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})", s)
    if m:
        d = date(t.year, int(m.group(1)), int(m.group(2)))
        return d if d >= t - timedelta(days=180) else date(t.year + 1, d.month, d.day)
    return date.fromisoformat(s)


def cats_str(x):
    return f"{x:+.2f} cats"


def pos_set(p):
    return set((p.get("eligible") or "").split("/")) - {"", "G", "F", "UT"}


# ------------------------------------------------------------------ data with Mike's corrections
class Ctx:
    def __init__(self, a):
        self.now = datetime.fromisoformat(a.now).replace(tzinfo=CT) if a.now else now_ct()
        self.snap = Path(a.snap)
        self.teams = json.loads(Path(a.teams).read_text()) if a.teams and Path(a.teams).exists() else {}
        self.notes = []
        self.orig_snap = self.snap
        self.snap = self._pending(a)
        self.D = MU.Data(self.snap)
        self.X = WP.Extra(self.snap)
        self.day = parse_day(a.date, self.now)
        self.changed = bool(a.added or a.dropped or a.taken)
        for q in a.taken or []:
            p = self.find(q)
            self.D.vals[p["id"]] = dict(self.D.vals[p["id"]], status="ONTEAM", on_team_id="?")
        for q in a.dropped or []:
            p = self.find(q)
            self.D.roster = [r for r in self.D.roster if not (r["team_id"] == self.D.my_tid and r["player_id"] == p["id"])]
            self.D.vals[p["id"]] = dict(self.D.vals[p["id"]], status="DROPPED")
        for q in a.added or []:
            p = self.find(q)
            self.D.roster = [r for r in self.D.roster if r["player_id"] != p["id"]]
            self.D.roster.append({"team_id": self.D.my_tid, "player_id": p["id"], "name": p["name"], "slot": "BE",
                                  "acquisition_type": "ADD", "acquired_ct": self.now.isoformat(timespec="minutes"),
                                  "injury_status": self.D.vals[p["id"]].get("injury_status", "")})
            self.D.vals[p["id"]] = dict(self.D.vals[p["id"]], status="ONTEAM", on_team_id=self.D.my_tid)

    def _pending(self, a):
        """Trades in ESPN review: the data job's snapshots/pending_trades.json plus the optional chat-kept file."""
        try:
            from relay import pending as PEND
            hours = 48
            try:
                hours = int(json.loads((self.snap / "league_settings.json").read_text()).get("trade", {})
                            .get("review_hours") or 48)
            except (OSError, ValueError, AttributeError):
                pass
            st = json.loads((self.snap / "status.json").read_text())
            trades, notes = PEND.active(self.snap, PEND.load(self.snap, a.pending), str(st.get("my_team_id")), hours)
            self.notes += notes
            if not trades:
                return self.snap
            day, mode = PEND.effect_day(self.snap, trades, self.now)
            desc = "; ".join(PEND.describe(t) for t in trades)
            if mode == "now":
                self.notes.append(f"Trade pending ({desc}). It should process before the next tip, so this answer "
                                  "treats it as done. If ESPN still shows the old roster, ask again without it.")
                return PEND.materialize(self.snap, trades, str(st.get("my_team_id")))
            self.notes.append(f"Trade pending ({desc}). It processes later, so this answer uses the current roster.")
        except Exception as e:  # noqa: BLE001  (a pending-trade problem must never block a quick answer)
            self.notes.append(f"Pending trade file ignored ({type(e).__name__}).")
        return self.snap

    # names
    def team_name(self, tid):
        tid = str(tid or "")
        if not tid:
            return "unknown opponent"
        if tid == self.D.my_tid:
            return "your team"
        if tid == "?":
            return "another team (since the data run)"
        t = self.teams.get(tid) or {}
        return t.get("name") or (self.D.teams.get(tid) or {}).get("name") or f"team {tid}"

    def find(self, q):
        """Best match for a typed name: exact, then contains, then last name, then initials (wcj)."""
        nq = norm(q)
        vals = self.D.vals
        tiers = [
            [pid for pid, v in vals.items() if norm(v["name"]) == nq],
            [pid for pid, v in vals.items() if nq and nq in norm(v["name"])],
            [pid for pid, v in vals.items() if nq and norm(v["name"]).split()[-1:] == nq.split()[-1:]],
            [pid for pid, v in vals.items() if nq.replace(" ", "") == initials(v["name"])],
        ]
        for hits in tiers:
            if hits:
                hits.sort(key=lambda pid: MU._num(vals[pid].get("rank"), 9999))
                if len(hits) > 1:
                    others = ", ".join(vals[h]["name"] for h in hits[1:4])
                    self.notes.append(f"'{q}' matched {vals[hits[0]]['name']} (also: {others}). Use the full name if wrong.")
                p = WP.mk(self.D, self.X, hits[0])
                return p
        raise SystemExit(f"No player matches '{q}'. Check the spelling.")

    def data_line(self):
        run = self.D.status.get("run_ct") or ""
        try:
            t = datetime.fromisoformat(run).astimezone(CT)
            mins = int((self.now - t).total_seconds() // 60)
            age = f"{mins // 60}h {mins % 60}m ago" if mins >= 60 else f"{mins}m ago"
            when = t.strftime("%a %-I:%M %p").replace(" 0", " ")
            line = f"Data: ESPN pull {when} CT ({age})."
            if mins > 12 * 60:
                line += " STALE: more than 12 hours old."
        except ValueError:
            line = "Data: run time unknown."
        line += " Moves made in ESPN after that are not reflected"
        line += " except the ones you passed in." if self.changed else "; a listed free agent may already be gone."
        return line

    # games
    def sp_of(self, iso):
        return next((sp for sp, d in self.D.sp_dates.items() if d == iso), None)

    def game(self, p, iso):
        sp = self.sp_of(iso)
        return self.D.games.get((sp, p["team"])) if sp else None

    def game_str(self, p, iso):
        g = self.game(p, iso)
        if not g:
            return "no game"
        vs = "vs" if g["home_away"] == "home" else "at"
        s = f"{vs} {g['opp']} {tip_label(g['tip_ct'])}"
        return s + (" (already tipped)" if tipped(iso, g["tip_ct"], self.now) else "")


# ------------------------------------------------------------------ engine wrappers
def setup(C):
    S = WP.setup(C.D, C.X, C.day)
    if S is None:
        raise SystemExit(f"No matchup days left on or after {C.day.isoformat()}.")
    return S


def matchup_line(C, S):
    O = S["O"]
    mp, prow, opp = S["mp"], S["prow"], S["opp"]
    exp = WP.exp_cats(O)
    close = [f"{c} {o['p']:.0%}" for c, o in O.items() if 0.4 < o["p"] < 0.6]
    under = [f"{c} {o['p']:.0%}" for c, o in O.items() if 0.15 < o["p"] <= 0.4]
    lost = [c for c, o in O.items() if o["p"] <= 0.15]
    s = (f"Week {mp} ({day_label(prow['start'])}-{day_label(prow['end'])}) vs {C.team_name(opp)}: "
         f"expected {exp:.1f} of 12 categories.")
    if close:
        s += f" Toss-ups: {', '.join(close)}."
    if under:
        s += f" Underdog: {', '.join(under)}."
    if lost:
        s += f" Likely lost: {', '.join(lost)}."
    return s


def week_exp(C, S, roster_fn):
    O2, _ = S["outlook"](roster_fn, S["key_fn"], True)
    return WP.exp_cats(O2)


def compare(C, S, drop, cands):
    """Expected categories this week for each candidate added now (dropping `drop`, or into an open spot when
    drop is None), minus keeping the current roster. Candidates whose game today already tipped do not count today;
    a dropped player whose game already tipped still counts today."""
    sps, active = S["sps"], S["active"]
    sp0, d0 = sps[0], C.D.sp_dates[sps[0]]
    base = [p for p in active if not drop or p["id"] != drop["id"]]
    drop_locked = bool(drop and C.game(drop, d0) and tipped(d0, C.game(drop, d0)["tip_ct"], C.now))
    keep = week_exp(C, S, lambda sp: active)
    out = []
    for x in cands:
        x_locked = bool(C.game(x, d0) and tipped(d0, C.game(x, d0)["tip_ct"], C.now))

        def roster(sp, x=x, x_locked=x_locked):
            r = list(base)
            if sp == sp0 and drop_locked:
                r.append(drop)
            if not (sp == sp0 and x_locked):
                r.append(x)
            return r
        out.append((week_exp(C, S, roster) - keep, x))
    out.sort(key=lambda t: -t[0])
    return keep, out


def streamer(C, S):
    return WP.occupant_of(C.D, S["active"])


def open_spots(C):
    return MU.roster_room(C.D, [])[0]


def fa_pool(C, S, day_only=True, pos=None, exclude=()):
    iso = C.day.isoformat()
    sp_day = C.sp_of(iso)
    pool = []
    for pid, v in C.D.vals.items():
        if v.get("status") not in FA or pid in exclude:
            continue
        p = WP.mk(C.D, C.X, pid)
        if not p or (pos and pos.upper() not in (v.get("eligible") or "").split("/")):
            continue
        games = [sp for sp in S["sps"] if MU.plays(C.D, p, sp) and WP.avail_on(p, C.D.sp_dates[sp]) > 0]
        if not games or (day_only and sp_day not in games):
            continue
        pool.append(p)
    # shortlist by the planner's per-day value, then score the shortlist exactly
    gain, _obj, _top = WP.candidate_gains(C.D, S["sps"], [p for p in S["active"]], {}, S["key_fn"], pool)
    pre = {}
    for (pid, _sp), g in gain.items():
        pre[pid] = pre.get(pid, 0.0) + g
    pool.sort(key=lambda p: -pre.get(p["id"], 0.0))
    return pool[:30]


def games_left(C, S, p):
    return sum(1 for sp in S["sps"] if MU.plays(C.D, p, sp) and WP.avail_on(p, C.D.sp_dates[sp]) > 0)


def inj_str(C, p):
    if not p.get("inj"):
        return ""
    r = C.X.inj.get(p["id"]) or {}
    s = f" [{(r.get('status') or p['inj']).replace('_', ' ').title()}"
    if r.get("detail"):
        s += f", {r['detail'].lower()}"
    if r.get("return_date"):
        s += f", back about {day_label(r['return_date'])}"
    return s + "]"


def cand_line(C, S, i, g, x):
    iso = C.day.isoformat()
    helps = "/".join(MU.helps(x, S["O"]))
    return (f"{i}. {x['name']} ({x['team']} {x['eligible']}){inj_str(C, x)}: {day_label(iso)} {C.game_str(x, iso)}; "
            f"{games_left(C, S, x)} games left; {cats_str(g)}" + (f"; helps {helps}" if helps else ""))


def drop_choice(C, S, a):
    """(player to drop or None for an open spot, sentence saying which)."""
    if a.for_name:
        p = C.find(a.for_name)
        mine = {q["id"]: q for q in S["active"]}
        if p["id"] not in mine:
            raise SystemExit(f"{p['name']} is not on your active roster.")
        return mine[p["id"]], f"in place of {p['name']} (your choice)"
    if open_spots(C) > 0:
        return None, "into your open roster spot (no drop)"
    s = streamer(C, S)
    if s:
        return s, f"in place of {s['name']}, the streaming spot"
    return None, "with no drop (no streaming spot found; pass --for NAME)"


# ------------------------------------------------------------------ commands
def cmd_today(C, a):
    S = setup(C)
    iso = S["sps"] and C.D.sp_dates[S["sps"][0]]
    out = [C.data_line(), matchup_line(C, S)]
    use_brief = not C.changed and C.day == C.now.date() and iso == C.day.isoformat()
    if use_brief:
        try:
            import brief as B   # tools/brief.py, the Daily Brief's own builder
            doc, _bd = B.build(C.orig_snap, "morning", C.teams or None, "daily", pending=a.pending)
            out.append("Brief's moves today (same engine as the Daily Brief page):")
            out += [f"- {x['text']}" for x in doc.get("actions", [])] or ["- none"]
            lu = doc["lineup"]
            out.append(f"Lineup {day_label(iso)} after those moves:")
        except Exception as e:  # noqa: BLE001
            use_brief = False
            out.append(f"(Brief actions unavailable: {type(e).__name__}. Lineup below is the current roster.)")
    if not use_brief:
        step = plan_step(C, iso)
        if step and not C.changed:
            out.append(f"Brief's plan for {day_label(iso)}: add {step['occupant']}"
                       + (f" for {step['drop']}" if step.get("drop") else "") + " that morning (lineup below is before it).")
        lu = MU._lineup(C.D, S["mine"], S["sps"][0], lambda p: S["key_fn"](p, iso), S["O"])
        out.append(f"Lineup {day_label(iso)}" + (" (with the changes you gave):" if C.changed else ":"))
    for p in lu["starters"]:
        g = {"opp": p.get("opp"), "tip_ct": p.get("tip"), "home_away": p.get("home_away")}
        vs = "vs" if g["home_away"] == "home" else "at"
        lock = " (already tipped)" if tipped(iso, g["tip_ct"], C.now) else ""
        out.append(f"  {p['slot']:<3} {p['name']} ({p['team']}) {vs} {g['opp']} {tip_label(g['tip_ct'])}{lock}"
                   + (f" [{p['inj'].replace('_', ' ').lower()}]" if p.get("inj") else ""))
    if lu.get("bench"):
        out.append("  Bench (has a game, no slot): " + ", ".join(p["name"] for p in lu["bench"]))
    n_open = lu.get("open_slots") if isinstance(lu.get("open_slots"), int) else len(lu.get("open_slots") or [])
    if n_open:
        out.append(f"  Empty lineup slots: {n_open}")
    ng = [p["name"] for p in lu.get("no_game", []) if p.get("why") != "out"]
    outp = [p["name"] + inj_str(C, {"id": p["id"], "inj": p.get("inj") or "OUT"}) for p in lu.get("no_game", [])
            if p.get("why") == "out"]
    if ng:
        out.append("  No game: " + ", ".join(ng))
    if outp:
        out.append("  Out: " + ", ".join(outp))
    if lu.get("ir"):
        out.append("  IR: " + ", ".join(p["name"] for p in lu["ir"]))
    return out


def plan_step(C, iso, pid=None):
    """The brief's streaming swap planned for date iso (or the swap that brings in pid), from brief_data.json."""
    try:
        bd = json.loads((C.snap / "brief_data.json").read_text())
        days = bd["week_plan"]["stream_plan"]["plans"]["daily"]["days"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    for d in days:
        if d.get("swap") and ((pid and d.get("id") == pid) or (not pid and d.get("date") == iso)):
            return d
    return None


def cmd_games(C, a):
    iso = C.day.isoformat()
    sp = C.sp_of(iso)
    if not sp:
        return [f"No NBA schedule found for {day_label(iso)}."]
    mp, _sps, _prow = MU.current_period(C.D, C.day)
    opp = MU.opponent(C.D, mp) if mp else None
    mine = [p for p in MU.team_players(C.D, C.D.my_tid) if p["slot"] != "IR"]
    theirs = [p for p in MU.team_players(C.D, opp) if p["slot"] != "IR"] if opp else []
    seen, rows = set(), []
    for (s, team), g in C.D.games.items():
        if s != sp or g["game_id"] in seen:
            continue
        seen.add(g["game_id"])
        home, away = (team, g["opp"]) if g["home_away"] == "home" else (g["opp"], team)
        teams = {home, away}
        m = [p["name"] + ("*" if p["avail"] == 0 else "") for p in mine if p["team"] in teams]
        o = [p["name"] + ("*" if p["avail"] == 0 else "") for p in theirs if p["team"] in teams]
        rows.append((g["tip_ct"] or "99:99", f"  {tip_label(g['tip_ct'])}  {away} at {home}"
                     + (" (tipped)" if tipped(iso, g["tip_ct"], C.now) else "")
                     + (f" | you: {', '.join(m)}" if m else "") + (f" | opp: {', '.join(o)}" if o else "")))
    rows.sort()
    out = [C.data_line(), f"{day_label(iso)}: {len(rows)} NBA games. First tip {tip_label(rows[0][0]) if rows else 'none'} CT."]
    out += [r for _t, r in rows]
    n_m = sum(1 for p in mine if p["avail"] > 0 and any(p["team"] in r for _t, r in rows))
    out.append(f"Your players with games: {n_m}. Opponent ({C.team_name(opp)}): "
               f"{sum(1 for p in theirs if p['avail'] > 0 and C.game(p, iso))}. * = listed out.")
    return out


def cmd_fa(C, a, exclude=(), title=None, drop=None, why=None):
    S = setup(C)
    if drop is None and why is None:
        drop, why = drop_choice(C, S, a)
    pool = fa_pool(C, S, day_only=not a.week, pos=a.pos, exclude=exclude)
    keep, ranked = compare(C, S, drop, pool)
    out = [C.data_line(), matchup_line(C, S),
           title or f"Best adds for {day_label(C.day.isoformat())} {why} (keeping everyone: {keep:.2f} of 12):"]
    for i, (g, x) in enumerate(ranked[:a.n], 1):
        out.append(cand_line(C, S, i, g, x))
    if not ranked:
        out.append("No free agent with a game fits.")
    elif ranked[0][0] < 0.03:
        out.append("None of these is a real gain over your current roster. Holding is fine.")
    return out, S, ranked


def cmd_alt(C, a):
    t = C.find(a.name)
    S = setup(C)
    mine = {p["id"]: p for p in S["active"]}
    plan_day = plan_step(C, None, t["id"])
    if plan_day and not a.date:
        C.day = date.fromisoformat(plan_day["date"])
        S = setup(C)
        mine = {p["id"]: p for p in S["active"]}
    head = []
    if t["id"] in mine and not a.for_name:
        drop, why = mine[t["id"]], f"in place of {t['name']}"
        head.append(f"Replacing {t['name']} ({t['team']} {t['eligible']}){inj_str(C, t)}.")
    else:
        v0 = C.D.vals[t["id"]]
        C.D.vals[t["id"]] = dict(v0, status="ONTEAM", on_team_id="?")
        if plan_day and plan_day.get("drop_id") and plan_day["drop_id"] in mine and not a.for_name:
            drop = mine[plan_day["drop_id"]]
            why = f"in place of {drop['name']} (the brief's planned drop)"
        else:
            drop, why = drop_choice(C, S, a)
        was_fa = v0.get("status") in FA
        where = ("the data still lists him as a free agent, so treating him as taken" if was_fa
                 else f"already on {C.team_name(v0.get('on_team_id'))} in the data")
        head.append(f"{t['name']} ({t['team']} {t['eligible']}): {where}"
                    + (f". The brief planned him for {day_label(plan_day['date'])}." if plan_day else "."))
        if was_fa:
            _k, r0 = compare(C, S, drop, [t])
            head.append(f"For reference, he was worth {cats_str(r0[0][0])} {why}.")
    out, S, ranked = cmd_fa(C, a, exclude={t["id"]}, drop=drop, why=why,
                            title=f"Best replacements for {day_label(C.day.isoformat())} {why}:")
    out[2:2] = head
    shown = {x["id"] for _g, x in ranked[:a.n]}
    want = pos_set(t)
    same = [(g, x) for g, x in ranked if x["id"] not in shown and pos_set(x) & want][:3]
    if same:
        out.append(f"Same position ({'/'.join(sorted(want))}):")
        out += [cand_line(C, S, i, g, x) for i, (g, x) in enumerate(same, len(shown) + 1)]
    return out


def cmd_player(C, a):
    p = C.find(a.name)
    v = C.D.vals[p["id"]]
    S = setup(C)
    on = next((r for r in C.D.roster if r["player_id"] == p["id"]), None)
    if on:
        own = C.team_name(on["team_id"]) + (f", slot {on.get('slot')}" if on["team_id"] == C.D.my_tid else "")
    elif v.get("status") in FA:
        own = "free agent (as of the data run)"
    else:
        own = C.team_name(v.get("on_team_id")) if v.get("status") == "ONTEAM" else (v.get("status") or "unknown").lower()
    out = [C.data_line(), f"{p['name']} ({p['team']} {p['eligible']}): {own}. Rank {p['rank']}, "
           f"rostered in {MU._num(v.get('pct_owned')):.0f}% of ESPN leagues."]
    r = C.X.inj.get(p["id"])
    if r:
        out.append(f"Injury: {r.get('status')}" + (f", {r['detail'].lower()}" if r.get("detail") else "")
                   + (f", expected back {day_label(r['return_date'])}" if r.get("return_date") else "")
                   + (f" (report {r['updated'][:10]})" if r.get("updated") else "") + ".")
        if r.get("comment"):
            out.append(f"Report note (paraphrase, never quote): {r['comment'][:240]}")
    else:
        out.append("Injury: none listed.")
    games = []
    for sp in S["sps"]:
        d = C.D.sp_dates[sp]
        if MU.plays(C.D, p, sp):
            games.append(f"{day_label(d)} {C.game_str(p, d)}")
    out.append(f"Games left this week ({len(games)}): " + ("; ".join(games) if games else "none"))
    L = p["line"]
    pct = lambda m, att: f"{L[m] / L[att]:.3f}" if L[att] >= 0.3 else "-"  # noqa: E731
    out.append(f"Per game: {L['PTS']:.1f} pts, {L['REB']:.1f} reb, {L['AST']:.1f} ast, {L['STL']:.1f} stl, "
               f"{L['BLK']:.1f} blk, {L['3PM']:.1f} 3pm, FG {pct('FGM', 'FGA')}, FT {pct('FTM', 'FTA')}, "
               f"3PT {pct('3PM', '3PA')}, DD {L['DD']:.2f}/game.")
    h = MU.helps(p, S["O"])
    out.append("Helps you most this week in: " + (", ".join(h) if h else "nothing in particular"))
    if v.get("status") in FA:
        drop, why = drop_choice(C, S, a)
        keep, ranked = compare(C, S, drop, [p])
        out.append(f"Adding him now {why}: {cats_str(ranked[0][0])} this week.")
    news = [n for n in MU._rows(C.snap / "news.csv") if p["id"] in (n.get("athlete_ids") or "").split(";")
            or p["id"] in (n.get("athlete_ids") or "").split(",")]
    news.sort(key=lambda n: n.get("published", ""), reverse=True)
    for n in news[:2]:
        out.append(f"News {n['published'][:10]} (paraphrase): {n['headline']}")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("snap")
    ap.add_argument("cmd", choices=["today", "games", "fa", "alt", "player"])
    ap.add_argument("name", nargs="*", help="player name for alt and player")
    ap.add_argument("--date")
    ap.add_argument("--taken", action="append")
    ap.add_argument("--added", action="append")
    ap.add_argument("--dropped", action="append")
    ap.add_argument("--for", dest="for_name")
    ap.add_argument("--pos")
    ap.add_argument("--week", action="store_true")
    ap.add_argument("-n", type=int, default=6)
    ap.add_argument("--teams")
    ap.add_argument("--pending")
    ap.add_argument("--now", help=argparse.SUPPRESS)  # tests: pretend it is this Central time
    a = ap.parse_args(argv)
    a.name = " ".join(a.name)
    if a.cmd in ("alt", "player") and not a.name:
        ap.error(f"{a.cmd} needs a player name")
    C = Ctx(a)
    if a.cmd == "today":
        out = cmd_today(C, a)
    elif a.cmd == "games":
        out = cmd_games(C, a)
    elif a.cmd == "fa":
        out = cmd_fa(C, a)[0]
    elif a.cmd == "alt":
        out = cmd_alt(C, a)
    else:
        out = cmd_player(C, a)
    print("\n".join(out + [f"Note: {n}" for n in C.notes]))


if __name__ == "__main__":
    main()
