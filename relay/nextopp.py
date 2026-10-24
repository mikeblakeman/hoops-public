"""Next opponent: a scouting report and a streaming carry-over check for the last days of a matchup.

Shown only in the last WINDOW_DAYS days of the current matchup (Mike's choice, Oct 24 2026).

- forecast: next matchup's category odds, simulated day by day from today's rosters (my roster as it will stand
  entering Monday, before any next-week streaming; the opponent starts its best players by generic value).
- form: how the opponent has played. This week's category totals and their rank in the league, their category
  record so far, their roster's production vs projection on games played, and who is hot or cold.
- roster: their key players (with next-week games) and injuries (with return dates and games they miss).
- carry: whether the streaming spot's end-of-week holder should change because of next week.

What a carry can gain: adds are unlimited and instant with no waivers, so a player wanted next week could also be
added Monday morning. Carrying him from the weekend saves that Monday add (the plan's swap cost: 0.03 categories in
the daily plan, 0.10 in the low-effort plan) and secures him before another manager streams him (not modeled). So in
practice a carry is a tie-break between weekend options that are close this week.

No-contradiction rule (Mike, Oct 24 2026): the current matchup comes first. A carry may change the current week's
streaming plan only if it costs this week at most CARRY_MAX_COST expected categories, and only when next week gains
more than that cost plus any extra add. The carry is written INTO the current week's stream plan (weekplan.py), so
the actions, the Week ahead card and the next-opponent card all read one plan. Only the streaming spot is touched.
"""
from datetime import date, timedelta
from pathlib import Path

from . import matchup as MU

WINDOW_DAYS = 3          # the card shows on the last 3 days of a matchup (Fri to Sun for a Mon-Sun week)
CARRY_MAX_COST = 0.05    # most expected current-week categories a next-week move may cost
CARRY_MIN_NET = 0.005    # next-week gain, net of that cost and swap effort, needed to change the plan (noise floor)
N_CANDIDATES = 12        # free agents tried as the end-of-week streamer


def in_window(prow, today):
    end = date.fromisoformat(prow["end"])
    return today >= end - timedelta(days=WINDOW_DAYS - 1)


def next_period(D, mp):
    for r in D.cal:
        if int(r["matchup_period"]) == mp + 1:
            sps = [sp for sp in range(int(r["first_sp"]), int(r["last_sp"]) + 1) if sp in D.sp_dates]
            return r, sps
    return None, []


# ------------------------------------------------------------------ forecast
def forecast(WP, D, mine, theirs, nsps):
    """Next matchup odds from zero. Returns (odds, key_fn, my expected games, their expected games)."""
    gen = lambda p, d: p["value"]  # noqa: E731
    zero = {c: 0.0 for c in MU.COMPONENTS}
    ro, vo, go = WP.simulate_days(D, lambda sp: theirs, nsps, gen)

    def outlook(key_fn, bn=False):
        rm, vm, gm = WP.simulate_days(D, lambda sp: mine, nsps, key_fn, bn)
        return MU.odds(D.cats, zero, zero, rm, vm, ro, vo), gm

    O, gm = outlook(gen)
    for _ in range(2):   # same two-pass weighting as the current-week planner
        key_fn = (lambda O_: (lambda p, d: WP.mv0(p, O_) * WP.avail_on(p, d)))(O)
        O, gm = outlook(key_fn, True)
    key_fn = (lambda O_: (lambda p, d: WP.mv0(p, O_) * WP.avail_on(p, d)))(O)
    return O, key_fn, gm, go


def stream_value(WP, path, nsps, gain, start_id, cost):
    """Planner's estimate for one streaming path: summed day gains minus swap effort."""
    v, prev = 0.0, start_id
    for i, sp in enumerate(nsps):
        v += gain.get((path[i], sp), 0.0)
        if path[i] != prev:
            v -= cost
        prev = path[i]
    return v


# ------------------------------------------------------------------ carry
def carry(WP, D, ctx):
    """Best end-of-week streaming change for next week, within the no-contradiction rule.

    ctx: sps (current remaining), path (current plan), occ_id, base (my roster without the streaming spot),
         names (id -> player), exact(path) -> current-week expected cats, plan_exp, nsps, gain_n, top_n, key_n,
         pool_n (next-week free agents), window_start (ISO date), swap_cost.
    Returns (new_path or None, info)."""
    sps, path, occ_id = ctx["sps"], ctx["path"], ctx["occ_id"]
    nsps, gain_n, top_n, cost = ctx["nsps"], ctx["gain_n"], ctx["top_n"], ctx["swap_cost"]

    def V(pid):
        p = WP.dp_path(nsps, gain_n, top_n, pid, cost)
        return stream_value(WP, p, nsps, gain_n, pid, cost)

    end_id = path[-1] if path else occ_id
    v_end = V(end_id)
    # candidates: free agents who help most next week, plus everyone already in this week's plan
    ranked = sorted({p["id"] for _g, p in [t for sp in nsps for t in top_n[sp]]},
                    key=lambda pid: -sum(gain_n.get((pid, sp), 0.0) for sp in nsps))[:N_CANDIDATES]
    cands = [pid for pid in dict.fromkeys(ranked + [x for x in path if x] + ([occ_id] if occ_id else []))
             if pid in ctx["names"]]

    def swaps(pth):
        n, prev = 0, occ_id
        for x in pth:
            n += x != prev
            prev = x
        return n

    base_swaps = swaps(path)
    best = None
    tried = 0
    for i, sp in enumerate(sps):
        if D.sp_dates[sp] < ctx["window_start"]:
            continue
        held = path[i - 1] if i else occ_id
        for x in cands:
            if x in path[:i] and x != held:      # a dropped player is not re-added
                continue
            if x == occ_id and held != occ_id:
                continue
            new = path[:i] + [x] * (len(sps) - i)
            if new == path:
                continue
            tried += 1
            e = ctx["exact"](new)
            loss = ctx["plan_exp"] - e
            if loss > CARRY_MAX_COST:
                continue
            extra = cost * (swaps(new) - base_swaps)
            next_gain = V(x) - v_end
            net = next_gain - max(loss, 0.0) - extra
            if best is None or net > best[0]:
                best = (net, new, x, i, loss, next_gain, e)
    info = {"rule": f"This week comes first: a next-week move may cost this week at most {CARRY_MAX_COST:.2f} "
                    f"expected categories, and must gain next week more than that cost plus any extra add.",
            "max_cost": CARRY_MAX_COST, "min_net": CARRY_MIN_NET, "options_tried": tried,
            "end_holder_id": end_id, "end_holder": (ctx["names"].get(end_id) or {}).get("name") if end_id else None,
            "changed": False}
    if best and best[0] > CARRY_MIN_NET:
        net, new, x, i, loss, next_gain, e = best
        p = ctx["names"][x]
        info.update(changed=True, player_id=x, player=p["name"], team=p["team"], pos=p["eligible"],
                    from_date=D.sp_dates[sps[i]], this_week_cost=round(max(loss, 0.0), 3),
                    next_week_gain=round(next_gain, 3), net=round(net, 3), expected_cats_this_week=round(e, 2),
                    replaces=info["end_holder"])
        return new, info
    if best:
        info["best_rejected"] = {"player": ctx["names"][best[2]]["name"], "net": round(best[0], 3),
                                 "this_week_cost": round(max(best[4], 0.0), 3)}
    return None, info


# ------------------------------------------------------------------ form
def _rows(p):
    return MU._rows(p)


def _ratio(m, a):
    return m / a if a else None


def team_week_ranks(D):
    """This matchup's running totals for every team and each team's rank (1 = best) per category."""
    tot = {t: MU.current_totals(D, t) for t in (D.scores or {})}
    tot = {t: v for t, v in tot.items() if any(v.values())}
    out = {}
    for c in D.cats:
        def val(t):
            if c in MU.RATIO:
                m, a = MU.RATIO[c]
                return _ratio(tot[t][m], tot[t][a]) or 0.0
            return tot[t].get(c, 0.0)
        order = sorted(tot, key=val, reverse=True)
        for t in tot:
            out.setdefault(t, {})[c] = {"value": round(val(t), 3 if c in MU.RATIO else 1),
                                        "rank": order.index(t) + 1}
    return out, len(tot)


def cat_record(D, tid, mp):
    for m in D.matchups:
        if int(MU._num(m["matchup_period"])) != mp:
            continue
        for side, other in (("home", "away"), ("away", "home")):
            if str(m[f"{side}_team_id"]) == str(tid):
                w, l, t = (int(MU._num(m[f"{side}_cat_{k}"])) for k in ("w", "l", "t"))
                return {"vs_team_id": str(m[f"{other}_team_id"]), "won": w, "lost": l, "tied": t}
    return None


def production(snap, players, O):
    """Season so far vs the season-start projection for a list of players (games played only)."""
    snap = Path(snap)
    tfiles = sorted((snap / "history" / "totals").glob("*.csv"))
    pfiles = sorted((snap / "history" / "proj").glob("*.csv"))
    if not tfiles or not pfiles:
        return None
    tot = {r["player_id"]: r for r in _rows(tfiles[-1])}
    proj = {r["player_id"]: r for r in _rows(pfiles[0])}
    keys = ["PTS", "REB", "AST", "STL", "BLK", "3PM", "3PA", "FGM", "FGA", "FTM", "FTA", "DD", "TO"]
    act_sum = {k: 0.0 for k in keys}
    exp_sum = {k: 0.0 for k in keys}
    games, rows = 0.0, []
    for p in players:
        t, pr = tot.get(p["id"]), proj.get(p["id"])
        gp = MU._num((t or {}).get("GP"))
        if not t or not pr or gp < 1:
            continue
        a = {k: MU._num(t.get(k)) / gp for k in keys}
        e = {k: MU._num(pr.get(k)) for k in keys}
        games += gp
        for k in keys:
            act_sum[k] += a[k] * gp
            exp_sum[k] += e[k] * gp
        delta = mv_line(a, O) - mv_line(e, O)
        rows.append({"id": p["id"], "name": p["name"], "team": p["team"], "games": int(gp), "delta": round(delta, 3),
                     "actual": {k: round(a[k], 1) for k in ("PTS", "REB", "AST", "3PM", "STL", "BLK")},
                     "projected": {k: round(e[k], 1) for k in ("PTS", "REB", "AST", "3PM", "STL", "BLK")}})
    if not rows:
        return None
    cats = []
    for c in ("PTS", "REB", "AST", "3PM", "STL", "BLK", "FGM", "FTM", "DD"):
        if exp_sum[c] > 0:
            cats.append({"cat": c, "actual": round(act_sum[c], 1), "projected": round(exp_sum[c], 1),
                         "pct_of_projection": round(act_sum[c] / exp_sum[c], 2)})
    for c, (m, a) in MU.RATIO.items():
        ra, re = _ratio(act_sum[m], act_sum[a]), _ratio(exp_sum[m], exp_sum[a])
        if ra is not None and re is not None:
            cats.append({"cat": c, "actual": round(ra, 3), "projected": round(re, 3),
                         "diff": round(ra - re, 3)})
    rows.sort(key=lambda r: -r["delta"])
    hot = [r for r in rows if r["delta"] > 0.01][:3]
    cold = [r for r in reversed(rows) if r["delta"] < -0.01][:3]
    return {"games": int(games), "through": tfiles[-1].stem, "categories": cats, "hot": hot, "cold": cold}


def mv_line(line, O):
    """Category value of a per-game stat line under odds O (same pricing as the planner)."""
    v = 0.0
    for c, o in O.items():
        if c in MU.RATIO:
            m, att = MU.RATIO[c]
            if o["att_m"]:
                v += o["w"] * (line.get(m, 0.0) - o["mine"] * line.get(att, 0.0)) / o["att_m"]
        else:
            v += o["w"] * line.get(c, 0.0)
    return v


# ------------------------------------------------------------------ report
def report(WP, D, X, snap, mp, opp_next, nprow, nsps, mine_next, theirs, O, key_n, gm, go):
    gen = lambda p, d: p["value"]  # noqa: E731
    days = []
    for sp in nsps:
        d = D.sp_dates[sp]
        st, _, free = MU.assign(WP.day_players(D, mine_next, sp), D.slots, lambda p: key_n(p, d), True)
        ost, _, ofree = MU.assign(WP.day_players(D, theirs, sp), D.slots, lambda p: gen(p, d))
        days.append({"date": d, "nba_games": len({g["game_id"] for (s, _t), g in D.games.items() if s == sp}),
                     "my_expected_games": round(sum(WP.avail_on(p, d) for p, _ in st), 1),
                     "opp_expected_games": round(sum(WP.avail_on(p, d) for p, _ in ost), 1),
                     "my_open_slots": len(free), "opp_open_slots": len(ofree)})

    def games_next(p):
        return round(sum(WP.avail_on(p, D.sp_dates[sp]) for sp in nsps if MU.plays(D, p, sp)), 1)

    def sched_games(p):
        return sum(1 for sp in nsps if MU.plays(D, p, sp))

    key_players = sorted([p for p in theirs if p["slot"] != "IR"], key=lambda p: -p["value"])[:6]
    inj = []
    for p in theirs:
        if not p["inj"]:
            continue
        r = X.inj.get(p["id"]) or {}
        missed = sum(1 for sp in nsps if MU.plays(D, p, sp) and WP.avail_on(p, D.sp_dates[sp]) == 0)
        inj.append({"name": p["name"], "team": p["team"], "status": p["inj"], "return_date": p.get("ret") or None,
                    "rank": p["rank"], "scheduled_games": sched_games(p), "games_missed": missed,
                    "comment": r.get("comment", "")})
    inj.sort(key=lambda r: r["rank"])

    ranks, n_teams = team_week_ranks(D)
    cur_mp = mp
    rec = cat_record(D, opp_next, cur_mp)
    this_week = None
    if str(opp_next) in ranks:
        this_week = {"teams_ranked": n_teams, "record": rec,
                     "categories": [dict(cat=c, **ranks[str(opp_next)][c]) for c in D.cats]}
        strong = [c for c in D.cats if ranks[str(opp_next)][c]["rank"] <= 2]
        weak = [c for c in D.cats if ranks[str(opp_next)][c]["rank"] >= n_teams - 1]
        this_week.update(strong=strong, weak=weak)

    cats = [{"cat": c, "mine": round(O[c]["mine"], 3 if c in MU.RATIO else 1),
             "theirs": round(O[c]["theirs"], 3 if c in MU.RATIO else 1), "p": round(O[c]["p"], 3),
             "label": MU.label(O[c]["p"])} for c in D.cats]
    return {
        "matchup_period": int(nprow["matchup_period"]),
        "period": {"start": nprow["start"], "end": nprow["end"]},
        "opponent_team_id": str(opp_next),
        "forecast": {"expected_cats": round(sum(o["p"] for o in O.values()), 2),
                     "player_games": {"mine": round(gm, 1), "theirs": round(go, 1)},
                     "categories": cats,
                     "basis": "Both rosters as they stand entering Monday (your streaming spot included), each "
                              "team starting its best players every day. Next week's streaming is not counted."},
        "days": days,
        "form": {"this_week": this_week, "vs_projection": production(snap, theirs, O)},
        "roster": {"key_players": [{"name": p["name"], "team": p["team"], "pos": p["eligible"], "rank": p["rank"],
                                    "inj": p["inj"], "games_next_week": games_next(p),
                                    "scheduled_games": sched_games(p)} for p in key_players],
                   "injuries": inj},
    }

