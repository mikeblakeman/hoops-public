"""Week planner: plan every remaining day of the current matchup, not just today.

The matchup engine (matchup.py) simulates all remaining days but only reports
today's lineup. This module adds the day-by-day view:

- coverage: for each remaining day, how many of my lineup slots are filled, which
  positions are empty, who has a game but no slot (and the same for the opponent)
- stream_plan: who should hold the streaming roster spot on each remaining day
  (dynamic program over days; a swap costs SWAP_COST categories; a dropped player
  is not re-added). Two plans: "daily" (chase every gain) and "low_effort".
  A second streaming spot is evaluated by freeing the cheapest rank 60+ non-keeper.
- trades: league-wide 1-for-1 and 2-for-1 search, scored as each side's expected
  category wins per week over the next two matchup periods.

Differences from matchup.py: availability is by day (injury return dates from
injuries.csv) instead of one flat factor, and days of the current matchup that
are already over are banked at projected values while current_scores.json is
still all zero (the data feed lags the games).

Everything here is advisory and wrapped by the caller so a failure never breaks
the brief. Run alone:  python -m relay.weekplan snapshots [YYYY-MM-DD]
"""
import itertools
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from . import matchup as MU
from . import nextopp as NO

SWAP_COST = 0.03            # category-equivalents charged per streaming add (effort + lost player)
LOW_EFFORT_COST = 0.10
SECOND_SPOT_MIN_RANK = 60   # only players ranked this low (or worse) are dropped for a second spot
TRADE_MIN_GAIN = 0.05       # cats per week
HARD_OUT = {"OUT", "SUSPENSION", "INJURY_RESERVE"}


class Extra:
    """Snapshot files the matchup engine does not load."""

    def __init__(self, snap):
        snap = Path(snap)
        self.inj = {r["player_id"]: r for r in MU._rows(snap / "injuries.csv")}
        self.bio = {r["player_id"]: r for r in MU._rows(snap / "player_bio.csv")}
        self.keepers = {r["player_id"] for r in MU._rows(snap / "draft_picks.csv") if r.get("keeper") == "True"}


def mk(D, X, pid, slot=None):
    p = MU.make_player(D, pid, slot)
    if p:
        p["ret"] = (X.inj.get(str(pid)) or {}).get("return_date", "")
        p["age"] = (X.bio.get(str(pid)) or {}).get("age", "")
    return p


def avail_on(p, d):
    """Availability on ISO date d, using the injury report's return date (shared with the matchup engine)."""
    return MU.avail_on(p, d)


def team_players(D, X, tid):
    return [p for r in D.roster if r["team_id"] == str(tid)
            for p in [mk(D, X, r["player_id"], r.get("slot"))] if p]


def day_players(D, players, sp):
    d = D.sp_dates[sp]
    return [p for p in players if p["slot"] != "IR" and avail_on(p, d) > 0 and MU.plays(D, p, sp)]


def blank():
    return {c: 0.0 for c in MU.COMPONENTS}, {c: 0.0 for c in MU.COMPONENTS}


def add_day(mean, var, starters, d):
    games = 0.0
    for p, _ in starters:
        a = avail_on(p, d)
        games += a
        for c in MU.COMPONENTS:
            mean[c] += a * p["line"][c]
        for c, cv in MU.GAME_CV.items():
            var[c] += a * (cv * p["line"][c]) ** 2
        dd = p["line"]["DD"]
        var["DD"] += a * dd * (1 - dd)
        for _r, (m, att) in MU.RATIO.items():
            pct = p["line"][m] / p["line"][att] if p["line"][att] else 0
            k = m + "|shot"
            var[k] = var.get(k, 0.0) + a * (MU.SHOT_INFLATE ** 2) * p["line"][att] * pct * (1 - pct)
    return games


def simulate_days(D, roster_for_day, sps, key_fn, bench_negative=False):
    mean, var = blank()
    games = 0.0
    for sp in sps:
        d = D.sp_dates[sp]
        st, _, _ = MU.assign(day_players(D, roster_for_day(sp), sp), D.slots, lambda p: key_fn(p, d), bench_negative)
        games += add_day(mean, var, st, d)
    return mean, var, games


def mv0(p, O):
    """Expected category wins per full-availability game for this player, given odds O."""
    v = 0.0
    for c, o in O.items():
        if c in MU.RATIO:
            m, att = MU.RATIO[c]
            if o["att_m"]:
                v += o["w"] * (p["line"][m] - o["mine"] * p["line"][att]) / o["att_m"]
        else:
            v += o["w"] * p["line"].get(c, 0.0)
    return v


def brief_line(p):
    return {k: round(p["line"][k], 2) for k in ("PTS", "REB", "AST", "STL", "BLK", "3PM", "DD")}


def exp_cats(O):
    return sum(o["p"] for o in O.values())


# ------------------------------------------------------------------ streaming
def candidate_gains(D, sps, base, extras, key_fn, pool):
    """Per (player, day) gain in matchup value if the player holds one extra roster spot that day."""
    def dv(roster, sp):
        d = D.sp_dates[sp]
        st, _, _ = MU.assign(day_players(D, roster, sp), D.slots, lambda p: key_fn(p, d), True)
        return sum(key_fn(p, d) for p, _ in st)

    gain, obj, top = {}, {}, {}
    for sp in sps:
        d = D.sp_dates[sp]
        r0 = base + extras.get(sp, [])
        b = dv(r0, sp)
        scored = []
        for p in pool:
            if not (avail_on(p, d) > 0 and MU.plays(D, p, sp)):
                continue
            g = dv(r0 + [p], sp) - b
            gain[(p["id"], sp)] = g
            obj[p["id"]] = p
            scored.append((g, p))
        scored.sort(key=lambda t: -t[0])
        top[sp] = scored[:6]
    return gain, obj, top


def dp_path(sps, gain, top, occ_id, swap_cost):
    """Best occupant per day for one streaming spot. A dropped player is not re-added."""
    if not sps:
        return []
    # the current occupant (None = the spot is open) is always a state, so "change nothing" is an option;
    # before, an open spot had no "stay open" state and the plan forced an add even when every add hurt
    others = set()
    for sp in sps:
        for _g, p in top[sp]:
            others.add(p["id"])
    others.discard(occ_id)
    states = [occ_id] + sorted(others)
    NEG = -1e9
    dp, back = [{} for _ in sps], [{} for _ in sps]
    for i, sp in enumerate(sps):
        for s in states:
            g = gain.get((s, sp), 0.0)
            prevs = {occ_id: 0.0} if i == 0 else dp[i - 1]
            best, bp = NEG, None
            for ps, pv in prevs.items():
                if pv <= NEG / 2:
                    continue
                if s == occ_id and ps != occ_id:
                    continue
                val = pv + g - (0.0 if s == ps else swap_cost)
                if val > best:
                    best, bp = val, ps
            dp[i][s], back[i][s] = best, bp
    s = max(dp[-1], key=lambda k: dp[-1][k])
    path = []
    for i in range(len(sps) - 1, -1, -1):
        path.append(s)
        s = back[i][s]
    path.reverse()
    return path


# ------------------------------------------------------------------ trades
def trade_search(D, X, top_n=12):
    rows = sorted(D.cal, key=lambda r: int(r["matchup_period"]))
    cur = int(D.status.get("current_matchup_period") or 1)
    future = [r for r in rows if int(r["matchup_period"]) > cur][:2]
    hz = [sp for r in future for sp in range(int(r["first_sp"]), int(r["last_sp"]) + 1) if sp in D.sp_dates]
    teams = {r["team_id"]: [] for r in D.roster}
    me = D.my_tid
    if not hz or me not in teams or len(teams) < 3:
        return {"baseline_cats_per_week": {}, "protected_keepers": [], "n_candidates": 0, "top": []}
    for t in teams:
        teams[t] = team_players(D, X, t)
    gen = lambda p, d: p["value"]  # noqa: E731
    zero = {c: 0.0 for c in MU.COMPONENTS}

    def totals(players):
        m, v, _g = simulate_days(D, lambda sp: players, hz, gen)
        return m, v

    T = {t: totals(pl) for t, pl in teams.items()}

    def strength(t, tt):
        s = 0.0
        for o in tt:
            if o != t:
                O = MU.odds(D.cats, zero, zero, tt[t][0], tt[t][1], tt[o][0], tt[o][1])
                s += sum(x["p"] for x in O.values())
        return s / (len(tt) - 1)

    base = {t: strength(t, T) for t in T}
    protected = {p["id"] for p in teams[me] if p["id"] in X.keepers}
    my_pool = [p for p in teams[me] if p["slot"] != "IR" and p["id"] not in protected]
    fa_best = None
    for pid, v in D.vals.items():
        if v.get("status") in ("FREEAGENT", "WAIVERS"):
            p = mk(D, X, pid)
            if p and not p["inj"] and (fa_best is None or p["value"] > fa_best["value"]):
                fa_best = p
    res = []
    for ot in sorted(teams):
        if ot == me:
            continue
        theirs = [p for p in teams[ot] if p["slot"] != "IR"]
        targets = sorted([p for p in theirs if p["rank"] <= 160], key=lambda p: p["rank"])[:16]
        for k_out in (1, 2):
            for outs in itertools.combinations(my_pool, k_out):
                vout = sum(p["value"] for p in outs)
                out_ids = {p["id"] for p in outs}
                for tgt in targets:
                    if vout < 0.8 * tgt["value"]:      # the partner must see roughly fair value
                        continue
                    new_mine = [p for p in teams[me] if p["id"] not in out_ids] + [tgt]
                    if k_out == 2 and fa_best:
                        new_mine.append(fa_best)       # the freed spot gets the best free agent
                    their_new = [p for p in teams[ot] if p["id"] != tgt["id"]] + list(outs)
                    if k_out == 2:
                        worst = next((p for p in sorted([p for p in their_new if p["slot"] != "IR"],
                                                        key=lambda p: p["value"]) if p["id"] not in out_ids), None)
                        if worst:
                            their_new = [p for p in their_new if p["id"] != worst["id"]]
                    T2 = dict(T)
                    T2[me], T2[ot] = totals(new_mine), totals(their_new)
                    mine_gain = strength(me, T2) - base[me]
                    their_gain = strength(ot, T2) - base[ot]
                    if mine_gain > TRADE_MIN_GAIN and their_gain > 0.0:
                        res.append({
                            "partner_team_id": ot,
                            "give": [{"id": p["id"], "name": p["name"], "pos": p["eligible"], "team": p["team"],
                                      "rank": p["rank"], "age": p["age"]} for p in outs],
                            "get": {"id": tgt["id"], "name": tgt["name"], "pos": tgt["eligible"], "team": tgt["team"],
                                    "rank": tgt["rank"], "age": tgt["age"]},
                            "my_cats_per_week": round(mine_gain, 3),
                            "their_cats_per_week": round(their_gain, 3),
                            "value_given": round(vout, 1), "value_got": round(tgt["value"], 1),
                            "gets_extra_spot": k_out == 2})
    res.sort(key=lambda r: -(r["my_cats_per_week"] + 0.5 * min(r["their_cats_per_week"], 0.3)))
    seen, out = {}, []
    for r in res:
        if seen.get(r["partner_team_id"], 0) >= 3:
            continue
        seen[r["partner_team_id"]] = seen.get(r["partner_team_id"], 0) + 1
        out.append(r)
        if len(out) >= top_n:
            break
    return {"baseline_cats_per_week": {t: round(v, 2) for t, v in base.items()},
            "protected_keepers": sorted(p["name"] for p in teams[me] if p["id"] in protected),
            "n_candidates": len(res), "top": out}


# ------------------------------------------------------------------ main
def WP_avail(p, D, sp):
    return MU.plays(D, p, sp) and avail_on(p, D.sp_dates[sp]) > 0


def next_opponent(D, X, snap, today, mp, prow, sps, base, occ_id, names, fa_pool, plans, exact_fn, describe):
    """Scouting report on the next opponent plus the streaming carry-over check (relay/nextopp.py).
    Changes plans[...]["days"] in place when a carry passes the rule; records the check in plans[...]["carry"]."""
    nprow, nsps = NO.next_period(D, mp)
    opp_n = MU.opponent(D, mp + 1) if nprow else None
    if not nprow or not nsps or not opp_n:
        return {"error": "next matchup not in the calendar yet"}
    theirs = team_players(D, X, opp_n)

    def roster_with(pid):
        return base + ([names[pid]] if pid in names else [])

    end0 = (plans["daily"]["days"][-1]["id"] if plans["daily"]["days"] else occ_id)
    O_n, key_n, _gm, _go = NO.forecast(sys.modules[__name__], D, roster_with(end0), theirs, nsps)
    # next-week streaming candidates: the best free agents with a game next week, plus this week's plan players
    fa_n = [p for pid, v in D.vals.items() if v.get("status") in ("FREEAGENT", "WAIVERS")
            for p in [mk(D, X, pid)] if p and any(WP_avail(p, D, sp) for sp in nsps)]
    pool_n = sorted(fa_n, key=lambda p: -p["value"])[:150]
    plan_ids = {d["id"] for pl in plans.values() for d in pl["days"] if d.get("id")}
    pool_n += [names[i] for i in plan_ids if i in names and i not in {p["id"] for p in pool_n}]
    gain_n, obj_n, top_n = candidate_gains(D, nsps, base, {}, key_n, pool_n)
    for pid, p in obj_n.items():
        names.setdefault(pid, p)
    window_start = (date.fromisoformat(prow["end"]) - timedelta(days=NO.WINDOW_DAYS - 1)).isoformat()

    for name, pl in plans.items():
        path = [d["id"] for d in pl["days"]]
        ctx = dict(sps=sps, path=path, occ_id=occ_id, names=names, exact=exact_fn, plan_exp=exact_fn(path),
                   nsps=nsps, gain_n=gain_n, top_n=top_n, window_start=window_start, swap_cost=pl["swap_cost_assumed"])
        new, info = NO.carry(sys.modules[__name__], D, ctx)
        if new:
            days = describe(new)
            for d in days:
                if d["date"] >= info["from_date"] and d["swap"]:
                    d["carry"] = True
            pl.update(days=days, adds=sum(1 for d in days if d["swap"]), expected_cats=info["expected_cats_this_week"])
        holder = (new or path)[-1] if (new or path) else occ_id
        if holder in names:
            p = names[holder]
            info["monday_holder"] = {"id": holder, "name": p["name"], "team": p["team"], "pos": p["eligible"],
                                     "games_next_week": sum(1 for sp in nsps if MU.plays(D, p, sp)),
                                     "helps_next_week": MU.helps(p, O_n)}
        pl["carry"] = info

    end1 = plans["daily"]["days"][-1]["id"] if plans["daily"]["days"] else occ_id
    if end1 != end0:   # the forecast uses the roster Mike will actually have on Monday
        O_n, key_n, _gm, _go = NO.forecast(sys.modules[__name__], D, roster_with(end1), theirs, nsps)
    out = NO.report(sys.modules[__name__], D, X, snap, mp, opp_n, nprow, nsps, roster_with(end1), theirs,
                    O_n, key_n, _gm, _go)
    out["window_days"] = NO.WINDOW_DAYS
    return out


def setup(D, X, today):
    """Shared start of every plan: both rosters, scores so far (with banking), the opponent's simulated week, the
    matchup-weighted key and today's outlook. None when no matchup day is left. Used by plan() and by the strategy
    lab's second streaming spot (relay/strategies.py), so both read the same numbers."""
    mp, sps, prow = MU.current_period(D, today)
    if mp is None or not sps:
        return None
    t = today.isoformat()
    period_sps = range(int(prow["first_sp"]), int(prow["last_sp"]) + 1)
    past = [sp for sp in period_sps if D.sp_dates.get(sp, "9999") < t]
    opp = MU.opponent(D, mp)
    mine = team_players(D, X, D.my_tid)
    theirs = team_players(D, X, opp) if opp else []
    cur_m, cur_o = MU.current_totals(D, D.my_tid), MU.current_totals(D, opp)
    gen = lambda p, d: p["value"]  # noqa: E731

    banked = False
    if past and not any(cur_m.values()) and not any(cur_o.values()):
        for sp in past:
            d = D.sp_dates[sp]
            for who, cur in ((mine, cur_m), (theirs, cur_o)):
                mean, var = blank()
                st, _, _ = MU.assign(day_players(D, who, sp), D.slots, lambda p: gen(p, d))
                add_day(mean, var, st, d)
                for c in MU.COMPONENTS:
                    cur[c] += mean[c]
        banked = True

    active = [p for p in mine if p["slot"] != "IR"]
    ro, vo, _go = simulate_days(D, lambda sp: theirs, sps, gen)

    def outlook(roster_for_day, key_fn, bn=False):
        rm, vm, gm = simulate_days(D, roster_for_day, sps, key_fn, bn)
        return MU.odds(D.cats, cur_m, cur_o, rm, vm, ro, vo), gm

    solo = lambda sp: active  # noqa: E731
    O, _ = outlook(solo, gen)
    key_fn = lambda p, d: mv0(p, O) * avail_on(p, d)  # noqa: E731
    O, _ = outlook(solo, key_fn, True)
    key_fn = lambda p, d: mv0(p, O) * avail_on(p, d)  # noqa: E731
    O, _ = outlook(solo, key_fn, True)
    return dict(mp=mp, sps=sps, prow=prow, opp=opp, mine=mine, theirs=theirs, cur_m=cur_m, cur_o=cur_o,
                banked=banked, active=active, ro=ro, vo=vo, outlook=outlook, O=O, key_fn=key_fn, gen=gen)


def occupant_of(D, active):
    """The streaming spot's current holder: the most recent non-draft acquisition on the active roster."""
    occupant, latest = None, ""
    for r in D.roster:
        if r["team_id"] == D.my_tid and r.get("acquisition_type") not in ("DRAFT", "", None) \
                and r.get("slot") != "IR" and r.get("acquired_ct", "") >= latest:
            latest, occupant = r.get("acquired_ct", ""), next((p for p in active if p["id"] == r["player_id"]), None)
    return occupant


def plan(snap, today=None, D=None, with_trades=True, with_next=True, my_roster=None):
    """my_roster: optional paper roster for my team (strategy lab); see relay.matchup.Data."""
    D = D or MU.Data(snap, my_roster=my_roster)
    X = Extra(snap)
    if today is None:
        today = MU.today_ct()
    if isinstance(today, str):
        today = date.fromisoformat(today)
    t = today.isoformat()
    S = setup(D, X, today)
    if S is None:
        return {"error": "no remaining days in the matchup", "date": t}
    mp, sps, prow, opp = S["mp"], S["sps"], S["prow"], S["opp"]
    theirs, banked, active = S["theirs"], S["banked"], S["active"]
    outlook, O, key_fn, gen = S["outlook"], S["O"], S["key_fn"], S["gen"]
    base_exp = exp_cats(O)

    # ---- per-day coverage
    days = []
    for sp in sps:
        d = D.sp_dates[sp]
        st, be, free = MU.assign(day_players(D, active, sp), D.slots, lambda p: key_fn(p, d), True)
        ost, _, ofree = MU.assign(day_players(D, theirs, sp), D.slots, lambda p: gen(p, d))
        tips = sorted(g["tip_ct"] for (s, _t), g in D.games.items() if s == sp)
        n_games = len({g["game_id"] for (s, _t), g in D.games.items() if s == sp})
        days.append({
            "sp": sp, "date": d, "nba_games": n_games, "first_tip_ct": tips[0] if tips else None,
            "my_starters": len(st), "my_open_slots": free,
            "my_no_slot": [p["name"] for p, w in be if w == "no_slot"],
            "opp_starters": len(ost), "opp_open_slots": ofree,
            "my_expected_games": round(sum(avail_on(p, d) for p, _ in st), 2),
            "opp_expected_games": round(sum(avail_on(p, d) for p, _ in ost), 2),
            "starters": [{"name": p["name"], "slot": s} for p, s in st]})
    light = [x["date"] for x in days if len(x["my_open_slots"]) >= 4]

    # ---- streaming spot
    occupant = occupant_of(D, active)
    occ_id = occupant["id"] if occupant else None
    base = [p for p in active if p["id"] != occ_id]
    fa_pool = [p for pid, v in D.vals.items() if v.get("status") in ("FREEAGENT", "WAIVERS")
               for p in [mk(D, X, pid)] if p and any(avail_on(p, D.sp_dates[sp]) > 0 and MU.plays(D, p, sp) for sp in sps)]
    gain1, obj1, top1 = candidate_gains(D, sps, base, {}, key_fn, fa_pool + ([occupant] if occupant else []))

    def exact(base_roster, paths, objs):
        def team_for_day(sp):
            i = sps.index(sp)
            return base_roster + [objs[path[i]] for path in paths if path[i] in objs]
        O2, _ = outlook(team_for_day, key_fn, True)
        return exp_cats(O2)

    # candidate_gains only keeps players with a game in the window, so a current streamer with no game
    # this week (for example a big with a Friday start) would be missing; look him up here too
    names = dict(obj1)
    if occupant:
        names.setdefault(occ_id, occupant)

    def describe(path):
        out, prev = [], occ_id
        for i, sp in enumerate(sps):
            pid = path[i]
            p = names.get(pid)
            g = D.games.get((sp, p["team"])) if p else None
            out.append({"date": D.sp_dates[sp], "occupant": p["name"] if p else None, "id": pid,
                        "team": p["team"] if p else None, "pos": p["eligible"] if p else None,
                        "swap": pid != prev, "drop": (names[prev]["name"] if prev in names else None) if pid != prev else None,
                        "drop_id": (prev if prev in names else None) if pid != prev else None,
                        "day_gain": round(gain1.get((pid, sp), 0.0), 3),
                        "plays_vs": g["opp"] if g else None, "tip_ct": g["tip_ct"] if g else None,
                        "helps": MU.helps(p, O) if p else [], "rank": p["rank"] if p else None,
                        "line": brief_line(p) if p else {},
                        "games_left": sum(1 for s2 in sps if MU.plays(D, p, s2)) if p else 0,
                        "alternatives": [{"id": q["id"], "name": q["name"], "team": q["team"], "pos": q["eligible"],
                                          "gain": round(x, 3), "rank": q["rank"], "line": brief_line(q),
                                          "helps": MU.helps(q, O)}
                                         for x, q in top1[sp] if q["id"] != pid][:3]})
            prev = pid
        return out

    keep_path = [occ_id] * len(sps)
    keep_exp = exact(base, [keep_path], obj1) if occ_id else base_exp

    def checked_path(cost):
        """dp_path ranks paths by summed per-day gain estimates; the exact outlook can disagree (a poor shooter
        can cost more in FG% than his counting stats add). Never plan adds that the exact check says lose."""
        path = dp_path(sps, gain1, top1, occ_id, cost)
        e = exact(base, [path], obj1)
        return (path, e) if e >= keep_exp - 1e-9 else (keep_path, keep_exp)

    plans = {}
    for name, cost in (("daily", SWAP_COST), ("low_effort", LOW_EFFORT_COST)):
        path, e = checked_path(cost)
        dd = describe(path)
        plans[name] = {"swap_cost_assumed": cost, "adds": sum(1 for x in dd if x["swap"]),
                       "expected_cats": round(e, 2), "days": dd}
    path1 = checked_path(SWAP_COST)[0]

    second = None
    droppable = [p for p in base if p["id"] not in X.keepers and p["rank"] >= SECOND_SPOT_MIN_RANK]
    if droppable:
        e_with = exact(base, [path1], obj1)
        loss, d = min(((e_with - exact([q for q in base if q["id"] != p["id"]], [path1], obj1), p) for p in droppable),
                      key=lambda t: t[0])
        b2 = [q for q in base if q["id"] != d["id"]]
        pool2 = [p for p in fa_pool if p["id"] not in set(path1)]
        extras = {sp: [obj1[path1[i]]] for i, sp in enumerate(sps) if path1[i] in obj1}
        gain2, obj2, top2 = candidate_gains(D, sps, b2, extras, key_fn, pool2)
        path2 = dp_path(sps, gain2, top2, None, SWAP_COST)
        objs = dict(obj1)
        objs.update(obj2)
        e2 = exact(b2, [path1, path2], objs)
        second = {"drop": d["name"], "drop_weekly_cost_cats": round(loss, 3),
                  "expected_cats_with_two_spots": round(e2, 2),
                  "net_gain_vs_one_spot": round(e2 - plans["daily"]["expected_cats"], 3),
                  "days": [{"date": D.sp_dates[sp], "occupant": objs[path2[i]]["name"], "team": objs[path2[i]]["team"],
                            "pos": objs[path2[i]]["eligible"], "day_gain": round(gain2.get((path2[i], sp), 0.0), 3)}
                           for i, sp in enumerate(sps) if path2[i] in objs]}

    # ---- next opponent (last days of the matchup only): scouting report, and a carry-over check that may
    # change the END of this week's streaming plan. It edits the plans above in place, so the actions, the
    # Week ahead card and the next-opponent card all read one plan (see nextopp.py for the rule).
    nxt = None
    if with_next and NO.in_window(prow, today):
        try:
            nxt = next_opponent(D, X, snap, today, mp, prow, sps, base, occ_id, names, fa_pool, plans,
                                lambda pth: exact(base, [pth], names), describe)
        except Exception as e:  # noqa: BLE001  advisory; never lose the daily plan
            nxt = {"error": f"{type(e).__name__}: {e}"}

    cats = [{"cat": c, "mine": round(O[c]["mine"], 3 if c in MU.RATIO else 1),
             "theirs": round(O[c]["theirs"], 3 if c in MU.RATIO else 1), "p": round(O[c]["p"], 3),
             "label": MU.label(O[c]["p"])} for c in D.cats]
    out = {"date": t, "matchup_period": mp, "period": {"start": prow["start"], "end": prow["end"]},
           "banked_past_days_at_projection": banked, "opponent_team_id": opp,
           "expected_cats_now": round(base_exp, 2), "categories": cats, "days": days, "light_days": light,
           "stream_plan": {"occupant_now": occupant["name"] if occupant else None,
                           "expected_cats_keep": round(keep_exp, 2), "plans": plans, "second_spot": second},
           "next_opponent": nxt}
    if with_trades:
        try:
            out["trades"] = trade_search(D, X)
        except Exception as e:  # trades are a bonus; never lose the daily plan
            out["trades"] = {"error": f"{type(e).__name__}: {e}", "top": []}
    return out


if __name__ == "__main__":
    snap = sys.argv[1] if len(sys.argv) > 1 else "snapshots"
    day = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else None
    print(json.dumps(plan(snap, day), indent=1))
