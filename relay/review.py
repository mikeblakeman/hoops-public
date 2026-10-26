"""Prior-day review: how did yesterday go compared with the projection?

Actual: ESPN's official matchup scoreboard, day D = scoreboard(D+1) - scoreboard(D)
(the first day of a matchup starts from zero). Projected: the category totals the
model expects from each team's best lineup out of its roster that day, using the
pre-game projection. Your own players are listed line by line from the daily
stat lines.
"""
from datetime import date, timedelta
from pathlib import Path

from . import history as H
from . import matchup as MU

CATS = ["PTS", "FGM", "FTM", "3PM", "REB", "AST", "STL", "BLK", "DD", "FG%", "FT%", "3PT%"]
SHOW = ["PTS", "REB", "AST", "3PM", "STL", "BLK"]
SID = {"PTS": "0", "BLK": "1", "STL": "2", "AST": "3", "REB": "6", "FGM": "13", "FGA": "14",
       "FTM": "15", "FTA": "16", "3PM": "17", "3PA": "18", "DD": "37"}
OFF = {"BE", "IR"}


def _totals(t):
    """Category totals from component sums."""
    out = {c: t.get(c, 0.0) for c in ("PTS", "FGM", "FTM", "3PM", "REB", "AST", "STL", "BLK", "DD")}
    for c, (m, a) in {"FG%": ("FGM", "FGA"), "FT%": ("FTM", "FTA"), "3PT%": ("3PM", "3PA")}.items():
        out[c] = t[m] / t[a] if t.get(a) else None
    return out


def _sum(lines):
    keys = ("PTS", "REB", "AST", "STL", "BLK", "3PM", "3PA", "FGM", "FGA", "FTM", "FTA", "DD")
    return {k: sum(x.get(k, 0.0) for x in lines) for k in keys}


def _from_board(b):
    return {k: H.num((b or {}).get(sid)) for k, sid in SID.items()}


def _winner(a, b):
    if a is None or b is None or abs(a - b) < 1e-9:
        return "tie"
    return "me" if a > b else "opp"


def _r(x, c):
    return None if x is None else (round(x, 3) if "%" in c else round(x, 1))


def _best_lineup(D, rows, proj, sp, day):
    """Expected component sums from the best lineup the roster could field that day."""
    players = []
    for r in rows:
        if r.get("slot") == "IR":
            continue
        p = MU.make_player(D, r["player_id"])
        if not p or (sp, p["team"]) not in D.games:
            continue
        p["avail"] = MU.availability(r.get("injury_status"))
        if p["avail"] <= 0:
            continue
        if r["player_id"] in proj:
            p["line"] = {k: proj[r["player_id"]].get(k, 0.0) for k in H.PROJ_KEYS}
        players.append(p)
    st, _, _ = MU.assign(players, D.slots, lambda p: p["value"])
    comp = [{k: p["line"][k] * p["avail"] for k in p["line"]} for p, _ in st]
    return _sum(comp), len(st)


def yesterday(snap, day=None):
    snap = Path(snap)
    lines = H.daily_lines(snap)
    if not lines:
        return None
    day = day or max(lines)
    if day not in lines:
        return None
    D = MU.Data(snap)
    sp = next((s for s, d in D.sp_dates.items() if d == day), None)
    if sp is None:
        return None
    d0 = date.fromisoformat(day)
    mp, _, row = MU.current_period(D, d0)
    opp = MU.opponent(D, mp) if mp else None
    if not opp:
        return None
    if row and row["end"] == day:
        # Last day of a matchup: the next day's scoreboard already belongs to the new period (all zeros),
        # so "after minus before" would give negative stats. Skip the card rather than show wrong numbers.
        return None
    # official results for the day
    after = H.load_scores(snap, (d0 + timedelta(days=1)).isoformat())
    before = H.load_scores(snap, day)
    first_day = bool(row) and row["start"] == day
    if after is None or (before is None and not first_day):
        return None
    me_act = {k: _from_board(after.get(D.my_tid)).get(k, 0) - (0 if first_day else _from_board((before or {}).get(D.my_tid))[k])
              for k in SID}
    op_act = {k: _from_board(after.get(opp)).get(k, 0) - (0 if first_day else _from_board((before or {}).get(opp))[k])
              for k in SID}
    # projection
    proj, exact = H.proj_for(snap, day)
    lrows = H.rows(snap / "history" / "lineups" / f"{day}.csv")
    have = {r["team_id"] for r in lrows}
    approx_opp = opp not in have
    my_rows = [r for r in lrows if r["team_id"] == D.my_tid] or [r for r in D.roster if r["team_id"] == D.my_tid]
    op_rows = [r for r in lrows if r["team_id"] == opp] or [r for r in D.roster if r["team_id"] == opp]
    pe, n_me = _best_lineup(D, my_rows, proj, sp, day)
    oe, n_op = _best_lineup(D, op_rows, proj, sp, day)
    pe, oe, pa, oa = _totals(pe), _totals(oe), _totals(me_act), _totals(op_act)
    cats, pw, aw = [], 0, 0
    for c in CATS:
        pwin, awin = _winner(pe[c], oe[c]), _winner(pa[c], oa[c])
        pw += pwin == "me"
        aw += awin == "me"
        cats.append({"cat": c, "proj_me": _r(pe[c], c), "proj_opp": _r(oe[c], c),
                     "act_me": _r(pa[c], c), "act_opp": _r(oa[c], c),
                     "proj_win": pwin, "act_win": awin, "matched": pwin == awin})
    # your players, line by line
    players = []
    for r in my_rows:
        if r.get("slot") == "IR":
            continue
        pid = r["player_id"]
        p = proj.get(pid)
        plays = (sp, r.get("pro_team") or "") in D.games
        a = lines[day].get(pid)
        if not (a or (plays and r.get("slot") not in OFF)):
            continue
        players.append({"name": r.get("name", ""), "slot": r.get("slot"), "played": bool(a),
                        "proj": {k: round(p[k], 1) for k in SHOW} if p else None,
                        "act": ({k: round(a[k]) for k in SHOW} | {"MIN": round(a["MIN"])}) if a else None})
    order = {"BE": 99}
    players.sort(key=lambda x: (order.get(x["slot"], 0), -(x["act"] or {}).get("PTS", 0)))
    notes = []
    dnp = [p["name"] for p in players if not p["played"]]
    if dnp:
        notes.append("Scheduled to play but no stat line: " + ", ".join(dnp) + ".")
    played = [p for p in players if p["played"] and p["proj"] and p["slot"] not in OFF]
    if played:
        gap = sorted(played, key=lambda p: p["act"]["PTS"] - p["proj"]["PTS"])
        notes.append(f"Biggest PTS gaps: {gap[-1]['name']} ({gap[-1]['act']['PTS'] - gap[-1]['proj']['PTS']:+.0f}), "
                     f"{gap[0]['name']} ({gap[0]['act']['PTS'] - gap[0]['proj']['PTS']:+.0f}).")
    bench = [p for p in players if p["slot"] in OFF and p["played"]]
    if bench:
        top = max(bench, key=lambda p: p["act"]["PTS"])
        notes.append(f"On your bench and played: {top['name']} ({top['act']['PTS']} PTS).")
    return {"date": day, "opponent_team_id": opp, "projection_exact": exact, "approx_opp_lineup": approx_opp,
            "starters_projected": [n_me, n_op], "cats": cats, "proj_cats_won": pw, "act_cats_won": aw,
            "matched": sum(1 for x in cats if x["matched"]), "players": players, "notes": notes}
