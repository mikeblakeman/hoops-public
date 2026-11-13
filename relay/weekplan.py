"""Week planner: plan every remaining day of the current matchup, not just today.

The matchup engine (matchup.py) simulates all remaining days but only reports
today's lineup. This module adds the day-by-day view:

- coverage: for each remaining day, how many of my lineup slots are filled, which
  positions are empty, who has a game but no slot (and the same for the opponent)
- stream_plan: who should hold the streaming roster spot on each remaining day
  (dynamic program over days; a swap costs SWAP_COST categories; a dropped player
  is not re-added). Two plans: "daily" (chase every gain) and "low_effort".
  A second streaming spot is evaluated by freeing the cheapest rank 60+ non-keeper.
- trades: league-wide 1-for-1, 2-for-1, 1-for-2 and 2-for-2 search, scored as each side's expected
  category wins per week from the first matchup a trade agreed today could count in through the fantasy
  playoffs (playoff weeks count double); only deals that clearly help both sides reach `top`.

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
# Trade ideas for the brief's Trades card. A trade takes days to agree and ESPN holds it 48 hours for review, so the
# search scores rest-of-season value, not this week: every full matchup from the first one a trade agreed today could
# count in (TRADE_LEAD_DAYS) through the fantasy playoffs, with playoff weeks counting TRADE_PLAYOFF_WEIGHT times.
# Each matchup is scored on its own (expected categories won against every other team that week, from day-by-day
# lineups and injury return dates), then averaged, so the result reads "categories won a week".
# Every 1-for-1, 2-for-1, 1-for-2 and 2-for-2 deal with each other team is screened on three sample weeks, and the
# best TRADE_SCREEN are scored on the full season. Only deals that clearly help Mike (TRADE_SHOW_MIN) AND clearly
# help the partner (TRADE_PARTNER_MIN) reach `top`; the closest misses are kept in `near`.
TRADE_LEAD_DAYS = 4        # about 2 days to agree, plus ESPN's 48-hour review
TRADE_PLAYOFF_WEIGHT = 2.0 # each fantasy playoff week counts double: the title is won there
TRADE_THIN_WEEK = 0.6      # weeks with under 60% of the usual NBA games (NBA Cup dates not set yet) are skipped
TRADE_SCREEN = 80          # deals rescored on the full season after the quick screen
TRADE_TARGETS = 14         # each partner's best players considered
TRADE_SHOW_MIN = 0.20      # cats per week for Mike, rest of season (about 3 to 4 more category wins by the playoffs)
TRADE_PARTNER_MIN = 0.10   # cats per week for the partner: the offer must clearly help them too
TRADE_FAIR = 0.85          # the partner gets at least this share of the value over replacement they give up ...
TRADE_OVERPAY = (1.6, 1.5) # ... and Mike never gives more than 1.6x + 1.5 of the value he gets
TRADE_YOUNG = 24           # age at or under which a player carries keeper value (4 keepers in 2027 and 2028)


def _vorp(D, p):
    """Value over replacement per game, floored at 0: with unlimited adds a replacement-level player is free."""
    return max(MU._num((D.vals.get(p["id"]) or {}).get("vorp_pg"), 0.0), 0.0)


def _age(p):
    try:
        return int(float(p.get("age") or 0))
    except (TypeError, ValueError):
        return 0


def trade_horizon(D, today):
    """The matchups a trade agreed today would count in: full matchups starting on or after today +
    TRADE_LEAD_DAYS, through the last playoff week. Thin weeks (NBA Cup dates not yet on the schedule) are skipped.
    Returns (periods, info); each period is {mp, sps, weight, playoffs, start, end}."""
    land = today + timedelta(days=TRADE_LEAD_DAYS)
    rows = sorted(D.cal, key=lambda r: int(r["matchup_period"]))
    future = [r for r in rows if (r.get("start") or "") >= land.isoformat()]
    games = sorted(int(MU._num(r.get("nba_games"), 0)) for r in rows if int(MU._num(r.get("nba_games"), 0)) > 0)
    typical = games[len(games) // 2] if games else 0
    periods, skipped = [], []
    for r in future:
        sps = [sp for sp in range(int(r["first_sp"]), int(r["last_sp"]) + 1) if sp in D.sp_dates]
        n = int(MU._num(r.get("nba_games"), 0))
        if not sps:
            continue
        if typical and n < TRADE_THIN_WEEK * typical:
            skipped.append({"matchup": int(r["matchup_period"]), "nba_games": n})
            continue
        po = str(r.get("playoffs")) == "True"
        periods.append({"mp": int(r["matchup_period"]), "sps": sps, "playoffs": po,
                        "weight": TRADE_PLAYOFF_WEIGHT if po else 1.0, "start": r.get("start"), "end": r.get("end")})
    info = {"lands_by": land.isoformat(), "matchups": [x["mp"] for x in periods],
            "from_matchup": periods[0]["mp"] if periods else None, "from_date": periods[0]["start"] if periods else None,
            "to_matchup": periods[-1]["mp"] if periods else None, "weeks": len(periods),
            "playoff_weeks": sum(1 for x in periods if x["playoffs"]), "playoff_weight": TRADE_PLAYOFF_WEIGHT,
            "skipped": skipped, "days": sum(len(x["sps"]) for x in periods)}
    return periods, info


def _screen_periods(periods):
    """Three sample weeks for the quick screen: the first, the middle of the regular season, and a playoff week."""
    reg = [x for x in periods if not x["playoffs"]]
    po = [x for x in periods if x["playoffs"]]
    pick = []
    for x in (reg[:1] + reg[len(reg) // 2:len(reg) // 2 + 1] + po[len(po) // 2:len(po) // 2 + 1]):
        if x not in pick:
            pick.append(x)
    return pick or periods[:3]


def trade_search(D, X, top_n=6, today=None):
    today = today or MU.today_ct()
    if isinstance(today, str):
        today = date.fromisoformat(today)
    periods, hinfo = trade_horizon(D, today)
    teams = {r["team_id"]: [] for r in D.roster}
    me = D.my_tid
    empty = {"baseline_cats_per_week": {}, "protected_keepers": [], "n_checked": 0, "n_candidates": 0,
             "n_rescored": 0, "top": [], "near": [], "bar": {"mine": TRADE_SHOW_MIN, "theirs": TRADE_PARTNER_MIN},
             "horizon": hinfo}
    if not periods or me not in teams or len(teams) < 3:
        return empty
    for t in teams:
        teams[t] = team_players(D, X, t)
    pending = {r["player_id"] for r in D.roster if (r.get("pending_tx") or "").strip() not in ("", "[]")}
    gen = lambda p, d: p["value"]  # noqa: E731
    zero = {c: 0.0 for c in MU.COMPONENTS}

    def totals(players, pers):
        out = []
        for x in pers:
            m, v, _g = simulate_days(D, lambda sp: players, x["sps"], gen)
            out.append((m, v))
        return out

    def by_cat(t, tt, pers):
        """Weighted average over the weeks of the chance to win each category against every other team.
        Returns (all weeks, playoff weeks only or None)."""
        acc, accp, wsum, np_ = {}, {}, 0.0, 0
        for i, x in enumerate(pers):
            wk = {}
            for o in tt:
                if o != t:
                    O = MU.odds(D.cats, zero, zero, tt[t][i][0], tt[t][i][1], tt[o][i][0], tt[o][i][1])
                    for c, y in O.items():
                        wk[c] = wk.get(c, 0.0) + y["p"] / (len(tt) - 1)
            for c, v in wk.items():
                acc[c] = acc.get(c, 0.0) + x["weight"] * v
                if x["playoffs"]:
                    accp[c] = accp.get(c, 0.0) + v
            wsum += x["weight"]
            np_ += 1 if x["playoffs"] else 0
        return ({c: v / wsum for c, v in acc.items()},
                {c: v / np_ for c, v in accp.items()} if np_ else None)

    def strength(t, tt, pers):
        a, po = by_cat(t, tt, pers)
        return sum(a.values()), (sum(po.values()) if po is not None else None)

    protected = {p["id"] for p in teams[me] if p["id"] in X.keepers}
    my_pool = [p for p in teams[me] if p["slot"] != "IR" and p["id"] not in protected and p["id"] not in pending]
    fas = [p for pid, v in D.vals.items() if v.get("status") in ("FREEAGENT", "WAIVERS")
           for p in [mk(D, X, pid)] if p and not p["inj"]]
    fa_best = max(fas, key=lambda p: p["value"]) if fas else None
    pv = lambda ps: sum(_vorp(D, p) for p in ps)  # noqa: E731

    def worst(players, keep):
        c = [p for p in players if p["slot"] != "IR" and p["id"] not in keep]
        return min(c, key=lambda p: p["value"]) if c else None

    def rosters_after(ot, outs, ins):
        out_ids, in_ids = {p["id"] for p in outs}, {p["id"] for p in ins}
        new_mine = [p for p in teams[me] if p["id"] not in out_ids] + list(ins)
        their_new = [p for p in teams[ot] if p["id"] not in in_ids] + list(outs)
        my_drop = their_drop = None
        if len(ins) > len(outs):      # Mike gets the extra player: he drops his weakest, they add a free agent
            my_drop = worst(new_mine, protected | in_ids)
            if not my_drop:
                return None
            new_mine = [p for p in new_mine if p["id"] != my_drop["id"]]
            if fa_best:
                their_new.append(fa_best)
        elif len(outs) > len(ins):    # they get the extra player: they drop their weakest, Mike adds one
            their_drop = worst(their_new, out_ids)
            if their_drop:
                their_new = [p for p in their_new if p["id"] != their_drop["id"]]
            if fa_best:
                new_mine.append(fa_best)
        return new_mine, their_new, my_drop, their_drop

    # ---- quick screen on three sample weeks
    scr = _screen_periods(periods)
    Ts = {t: totals(pl, scr) for t, pl in teams.items()}
    base_s = {t: strength(t, Ts, scr)[0] for t in Ts}
    screened, n = [], 0
    for ot in sorted(teams):
        if ot == me:
            continue
        theirs = [p for p in teams[ot] if p["slot"] != "IR" and p["id"] not in pending]
        targets = sorted([p for p in theirs if p["rank"] <= 160], key=lambda p: p["rank"])[:TRADE_TARGETS]
        for k_out in (1, 2):
            for outs in itertools.combinations(my_pool, k_out):
                give_v = pv(outs)
                for k_in in (1, 2):
                    for ins in itertools.combinations(targets, k_in):
                        get_v = pv(ins)
                        if give_v < TRADE_FAIR * get_v or give_v > TRADE_OVERPAY[0] * get_v + TRADE_OVERPAY[1]:
                            continue
                        ra = rosters_after(ot, outs, ins)
                        if not ra:
                            continue
                        n += 1
                        T2 = dict(Ts)
                        T2[me], T2[ot] = totals(ra[0], scr), totals(ra[1], scr)
                        mg = strength(me, T2, scr)[0] - base_s[me]
                        tg = strength(ot, T2, scr)[0] - base_s[ot]
                        if mg > 0 and tg > 0:
                            screened.append((mg + 0.5 * min(tg, 0.3), ot, outs, ins, give_v, get_v))
    screened.sort(key=lambda r: -r[0])

    # ---- full season for the best of the screen
    T = {t: totals(pl, periods) for t, pl in teams.items()}
    base = {t: strength(t, T, periods) for t in T}
    base_cat = {t: by_cat(t, T, periods)[0] for t in T}
    res = []
    for _sc, ot, outs, ins, give_v, get_v in screened[:TRADE_SCREEN]:
        new_mine, their_new, my_drop, their_drop = rosters_after(ot, outs, ins)
        T2 = dict(T)
        T2[me], T2[ot] = totals(new_mine, periods), totals(their_new, periods)
        sm, smp = strength(me, T2, periods)
        st, stp = strength(ot, T2, periods)
        mine_gain, their_gain = sm - base[me][0], st - base[ot][0]
        if mine_gain > TRADE_MIN_GAIN and their_gain > 0.0:
            res.append({"ot": ot, "outs": outs, "ins": ins, "my_drop": my_drop, "their_drop": their_drop,
                        "mine": mine_gain, "theirs": their_gain, "give_v": give_v, "get_v": get_v,
                        "mine_po": None if smp is None else smp - base[me][1],
                        "theirs_po": None if stp is None else stp - base[ot][1],
                        "T2": (T2[me], T2[ot])})

    score = lambda r: r["mine"] + 0.5 * min(r["theirs"], 0.3)  # noqa: E731
    res.sort(key=lambda r: -score(r))

    def pl(p):
        return {"id": p["id"], "name": p["name"], "pos": p["eligible"], "team": p["team"], "rank": p["rank"],
                "age": p["age"], "inj": p["inj"], "ret": p.get("ret") or "", "young": 0 < _age(p) <= TRADE_YOUNG}

    def cat_moves(before, after):
        d = [{"cat": c, "delta": round(after[c] - before[c], 3)} for c in after]
        d = [x for x in d if abs(x["delta"]) >= 0.03]
        return sorted(d, key=lambda x: -x["delta"])

    rnd = lambda x: None if x is None else round(x, 3)  # noqa: E731

    def describe(r):
        T2 = dict(T)
        T2[me], T2[r["ot"]] = r["T2"]
        ratio = r["get_v"] and r["give_v"] / r["get_v"]
        return {
            "partner_team_id": r["ot"],
            "give": [pl(p) for p in r["outs"]],
            "gets": [pl(p) for p in r["ins"]],
            "get": pl(r["ins"][0]),   # older readers (one incoming player)
            "shape": f"{len(r['outs'])}-for-{len(r['ins'])}",
            "my_cats_per_week": round(r["mine"], 3),
            "their_cats_per_week": round(r["theirs"], 3),
            "my_playoff_cats_per_week": rnd(r["mine_po"]),
            "their_playoff_cats_per_week": rnd(r["theirs_po"]),
            "my_categories": cat_moves(base_cat[me], by_cat(me, T2, periods)[0]),
            "their_categories": cat_moves(base_cat[r["ot"]], by_cat(r["ot"], T2, periods)[0]),
            "value_given": round(r["give_v"], 1), "value_got": round(r["get_v"], 1),
            "value_label": "even" if not r["get_v"] or ratio <= 1.15 else "they get more",
            "my_drop": r["my_drop"] and pl(r["my_drop"]),
            "their_drop": r["their_drop"] and pl(r["their_drop"]),
            "my_add": fa_best and len(r["outs"]) > len(r["ins"]) and pl(fa_best) or None,
            "gets_extra_spot": len(r["outs"]) > len(r["ins"]),
        }

    top, near, seen_team, seen_in, seen_sets = [], [], {}, {}, set()
    for r in res:
        sig = r["mine"] >= TRADE_SHOW_MIN and r["theirs"] >= TRADE_PARTNER_MIN
        key = (r["ot"], frozenset(p["id"] for p in r["outs"]), frozenset(p["id"] for p in r["ins"]))
        if key in seen_sets:
            continue
        if sig and len(top) < top_n:
            # variety: at most 2 ideas per partner, one per incoming package, and the same star in at most 2
            star = max(r["ins"], key=lambda p: p["value"])["id"]
            pkg = frozenset(p["id"] for p in r["ins"])
            if seen_team.get(r["ot"], 0) >= 2 or seen_in.get(star, 0) >= 2 or pkg in seen_in:
                continue
            seen_team[r["ot"]] = seen_team.get(r["ot"], 0) + 1
            seen_in[star] = seen_in.get(star, 0) + 1
            seen_in[pkg] = 1
            seen_sets.add(key)
            top.append(describe(r))
        elif not sig and len(near) < 3 and r["theirs"] >= TRADE_PARTNER_MIN / 2:
            seen_sets.add(key)
            near.append(describe(r))
    return {"baseline_cats_per_week": {t: round(v[0], 2) for t, v in base.items()},
            "protected_keepers": sorted(p["name"] for p in teams[me] if p["id"] in protected),
            "bar": {"mine": TRADE_SHOW_MIN, "theirs": TRADE_PARTNER_MIN},
            "horizon": hinfo, "n_checked": n, "n_candidates": len(screened), "n_rescored": min(len(screened), TRADE_SCREEN),
            "top": top, "near": near}


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
            out["trades"] = trade_search(D, X, today=today)
        except Exception as e:  # trades are a bonus; never lose the daily plan
            out["trades"] = {"error": f"{type(e).__name__}: {e}", "top": []}
    return out


if __name__ == "__main__":
    snap = sys.argv[1] if len(sys.argv) > 1 else "snapshots"
    day = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else None
    print(json.dumps(plan(snap, day), indent=1))
