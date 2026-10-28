"""Roster strategies for the strategy lab (claude/engine/strategy-lab-spec.md, section 2).

Each strategy is one function decide(ctx, roster, protected, day) that returns today's moves for its paper team:
    {"ir": [ids], "adds": [ids], "drops": [ids], "bench": [ids], "notes": [...]}
(two_spots also returns "spot_b", schedule_stack "stacked").

`ctx` (Ctx below) gives the engine's view of the paper team: relay.matchup.Data with the paper roster swapped in
(the engine is not forked), the matchup context and, for `current`, the full brief_data the brief itself reads.
A decision reads only the snapshot directory it is given; relay/lab.py hands it a view of the snapshot that hides
every history file dated on or after the decision date.

Protected players (keepers plus the top 10 by season value in the Monday snapshot) are never dropped: decide()
filters them out here, and relay/lab.py refuses them again when it applies the moves.
"""
from pathlib import Path

from . import matchup as MU
from . import moves as MV
from . import weekplan as WP

ORDER = ["current", "fill_all", "smart_fill", "two_spots", "schedule_stack", "hold"]
CONTROL = "current"
REFERENCE = {"hold"}
INFO = {
    "current": ("Current: one streaming spot", "Week planner streams one spot almost daily (what the brief recommends)",
                "About 1 add a day"),
    "fill_all": ("Fill every slot", "Fill every open lineup slot daily with the best free agent who plays",
                 "2 to 5 adds a day"),
    "smart_fill": ("Smart fill", "Fill open slots only when an add gains 0.02+ expected categories; protect close "
                                 "percentages late in the week", "1 to 3 adds a day"),
    "two_spots": ("Two streaming spots", "Current plan plus a second streaming spot planned the same way",
                  "About 2 adds a day"),
    "schedule_stack": ("Monday schedule stack", "Monday: swap holds for free agents with the most games, then hold",
                       "1 to 3 adds a week"),
    "hold": ("Hold (reference)", "IR moves and open-spot fills only, no swaps", "Under 1 add a week"),
}
SMART_MIN_GAIN = 0.02
STACK_EDGE = 0.15
VOLUME = {"FG%": 10.0, "FT%": 4.0, "3PT%": 5.0}   # attempts per game that make a "volume shooter"
B_STAMP = "0000-00-00T00:00"                     # spot B adds never look like the newest pickup (spot A)


class Ctx:
    """The paper team's view of one decision day. `roster`: list of {player_id, slot, acquisition_type,
    acquired_ct}; `free_ids`: players it dropped (back in its own pool); `scores`: its matchup totals so far
    (component dict) or None to use the snapshot's; `state`: the strategy's lab state (read only)."""

    def __init__(self, snap, day, roster, free_ids=(), scores=None, state=None):
        self.snap, self.day = Path(snap), day
        self.roster, self.free_ids, self.scores = roster, tuple(free_ids), scores
        self.state = state or {}
        self._D = self._bd = self._c = None

    def data(self):
        if self._D is None:
            self._D = MU.Data(self.snap, my_roster=self.roster, my_scores=self.scores, free_ids=self.free_ids)
        return self._D

    def build(self):
        """brief_data for the paper team, built exactly as the data run builds it (no trade search)."""
        if self._bd is None:
            self._bd = MU.build(self.snap, self.day, D=self.data(), with_trades=False)
        return self._bd

    def context(self):
        if self._c is None:
            self._c = MU._context(self.data(), self.day)
        return self._c


def _ids(xs):
    out = []
    for x in xs:
        i = str(x["id"] if isinstance(x, dict) else x)
        if i and i not in out:
            out.append(i)
    return out


def result(ir=(), adds=(), drops=(), bench=(), notes=(), **extra):
    return dict({"ir": _ids(ir), "adds": _ids(adds), "drops": _ids(drops), "bench": _ids(bench),
                 "notes": list(notes)}, **extra)


def caps(D):
    ls = D.settings.get("lineup_slots") or {}
    return sum(int(v) for k, v in ls.items() if k != "IR"), int(ls.get("IR", 0))


def ir_candidates(D, players):
    """Active players ESPN shows OUT (or suspended), in the engine's order. IR is legal only for them."""
    return [p for p in players if p["slot"] != "IR" and (p.get("inj") or "").upper() in MU.HARD_OUT]


def churnable(players, protected):
    return [p for p in players if p["slot"] != "IR" and str(p["id"]) not in protected]


def games_left(D, p, sps):
    return sum(1 for sp in sps if MU.plays(D, p, sp) and MU.avail_on(p, D.sp_dates.get(sp, "")) > 0)


def free_agents(D):
    return [p for pid, v in D.vals.items() if v.get("status") in MU.FREE for p in [MU.make_player(D, pid)] if p]


# ------------------------------------------------------------------ strategies
def current(ctx, roster, protected, day):
    """Exactly what the brief recommends: the brief's own move selection (relay.moves.select_moves) on the engine
    output for this paper roster."""
    bd = ctx.build()
    if bd.get("error"):
        return result(notes=[f"engine: {bd['error']}"])
    sel = MV.select_moves(bd, bd.get("week_plan") or {}, "daily")
    return result(sel["ir_ids"], sel["add_ids"], sel["drop_ids"], notes=sel["text"])


def hold(ctx, roster, protected, day):
    """IR moves and open-spot fills only (reference: does streaming help at all?)."""
    bd = ctx.build()
    if bd.get("error"):
        return result(notes=[f"engine: {bd['error']}"])
    fills = bd.get("fills") or []
    return result([p["id"] for p in bd.get("ir_moves", [])], [f["add"]["id"] for f in fills],
                  notes=[f"{p['name']} to IR" for p in bd.get("ir_moves", [])] +
                        [f"add {f['add']['name']}" for f in fills])


def _fill(ctx, protected, smart):
    D, c = ctx.data(), ctx.context()
    if not c or not c["sps"]:
        return result(notes=["no matchup day"])
    sps, key, sp0 = c["sps"], c["key"], c["sps"][0]
    cap_active, cap_ir = caps(D)
    mine = [dict(p) for p in c["mine"]]
    n_ir = sum(1 for p in mine if p["slot"] == "IR")
    ir = ir_candidates(D, mine)[:max(0, cap_ir - n_ir)]
    for p in ir:
        p["slot"] = "IR"
    roster = list(mine)
    pool = sorted([p for p in free_agents(D) if p["avail"] > 0 and MU.plays(D, p, sp0)], key=key, reverse=True)
    adds, drops, notes = [], [], [f"{p['name']} to IR" for p in ir]

    def playing(r):
        return [p for p in r if p["slot"] != "IR" and p["avail"] > 0 and MU.plays(D, p, sp0)]

    def exp(r):
        O2, _, _ = MU.outlook(D, r, c["opp_sim"], sps, key, c["cur_m"], c["cur_o"], True)
        return sum(o["p"] for o in O2.values())

    base = exp(roster) if smart else 0.0
    for _ in range(10):
        st, _, free = MU.assign(playing(roster), D.slots, key, True)
        if not free:
            break
        # free agents who would actually take an open slot today, best first
        fits = []
        for x in pool:
            if x in adds:
                continue
            st2, _, _ = MU.assign(playing(roster) + [x], D.slots, key, True)
            if len(st2) > len(st):
                fits.append(x)
                if len(fits) >= (8 if smart else 1):
                    break
        if not fits:
            break
        drop = None
        if sum(1 for p in roster if p["slot"] != "IR") >= cap_active:
            idle = [p for p in churnable(roster, protected) if not (p["avail"] > 0 and MU.plays(D, p, sp0))]
            idle.sort(key=lambda p: (games_left(D, p, sps), p["value"]))
            if not idle:
                break
            drop = idle[0]
        r0 = [p for p in roster if p is not drop]
        if smart:
            scored = sorted(((exp(r0 + [x]) - base, x) for x in fits), key=lambda t: -t[0])
            gain, x = scored[0]
            if gain < SMART_MIN_GAIN:
                notes.append(f"no add gains {SMART_MIN_GAIN:.2f}+ (best {x['name']} {gain:+.3f})")
                break
            base += gain
        else:
            x = fits[0]
        roster = r0 + [x]
        adds.append(x)
        if drop is not None:
            drops.append(drop)
        notes.append(f"add {x['name']}" + (f" for {drop['name']}" if drop is not None else ""))

    bench = []
    if smart and len(sps) <= 2:
        bench, why = protect_percentages(D, c["O"], [p for p, _ in MU.assign(playing(roster), D.slots, key, True)[0]])
        notes += why
    return result(ir, adds, drops, bench, notes)


def protect_percentages(D, O, starters):
    """Last 2 days: when every counting category is settled (85%+ or 15%-) and a percentage category is close
    (40 to 60%), bench volume shooters who would lower it."""
    counting = [c for c in O if c not in MU.RATIO]
    if not counting or any(0.15 < O[c]["p"] < 0.85 for c in counting):
        return [], []
    close = [c for c in O if c in MU.RATIO and 0.4 <= O[c]["p"] <= 0.6]
    bench, why = [], []
    for c in close:
        m, att = MU.RATIO[c]
        for p in starters:
            line = p["line"]
            if line[att] >= VOLUME[c] and line[m] < O[c]["mine"] * line[att] and p not in bench:
                bench.append(p)
                why.append(f"bench {p['name']} to protect {c}")
    return bench, why


def fill_all(ctx, roster, protected, day):
    return _fill(ctx, protected, smart=False)


def smart_fill(ctx, roster, protected, day):
    return _fill(ctx, protected, smart=True)


def two_spots(ctx, roster, protected, day):
    """`current` for spot A, plus spot B: the lowest-value churnable hold at the Monday reset, planned day by day
    by the same planner after spot A's plan is fixed (spot A's players are not candidates)."""
    a = current(ctx, roster, protected, day)
    bd = ctx.build()
    wp = bd.get("week_plan") or {}
    if bd.get("error") or wp.get("error") or not wp.get("stream_plan"):
        return dict(a, spot_b=ctx.state.get("spot_b"))
    D = ctx.data()
    X = WP.Extra(ctx.snap)
    S = WP.setup(D, X, day)
    if S is None:
        return dict(a, spot_b=ctx.state.get("spot_b"))
    sps, key_fn, active = S["sps"], S["key_fn"], S["active"]
    occ_a = WP.occupant_of(D, active)
    path_a = [d.get("id") for d in wp["stream_plan"]["plans"]["daily"]["days"]]
    occ_b = ctx.state.get("spot_b")
    b_player = next((p for p in active if p["id"] == occ_b), None)
    if b_player is None:
        occ_b = None
    held = {occ_a["id"] if occ_a else None, occ_b}
    if len(path_a) != len(sps):
        return dict(a, spot_b=occ_b)
    fa_pool = [p for p in (WP.mk(D, X, pid) for pid, v in D.vals.items() if v.get("status") in MU.FREE)
               if p and p["id"] not in set(path_a) and str(p["id"]) not in set(a["adds"])
               and any(WP.WP_avail(p, D, sp) for sp in sps)]
    names = {p["id"]: p for p in fa_pool + active}
    base = [p for p in active if p["id"] not in held and p["id"] not in set(a["drops"])]
    extras = {sp: [names[path_a[i]]] for i, sp in enumerate(sps) if path_a[i] in names}
    gain, _obj, top = WP.candidate_gains(D, sps, base, extras, key_fn, fa_pool + ([b_player] if b_player else []))
    path_b = WP.dp_path(sps, gain, top, occ_b, WP.SWAP_COST)
    days, prev = [], occ_b
    for i, sp in enumerate(sps):
        p = names.get(path_b[i])
        days.append({"date": D.sp_dates[sp], "id": path_b[i], "occupant": p["name"] if p else None,
                     "swap": path_b[i] != prev, "drop_id": prev, "drop": names[prev]["name"] if prev in names else None,
                     "day_gain": gain.get((path_b[i], sp), 0.0)})
        prev = path_b[i]
    d0 = MV.defer_idle_swap(days)[0]
    if not d0["swap"] or not d0.get("id"):
        return dict(a, spot_b=occ_b, notes=a["notes"] + ([f"spot B: wait, add {days[1]['occupant']} tomorrow"]
                                                         if d0.get("deferred_to") else []))
    drops = a["drops"] + ([d0["drop_id"]] if d0.get("drop_id") else [])
    return dict(a, adds=a["adds"] + [str(d0["id"])], drops=_ids(drops), spot_b=str(d0["id"]),
                notes=a["notes"] + [f"spot B: add {d0['occupant']}" + (f" for {d0['drop']}" if d0.get("drop") else "")])


def schedule_stack(ctx, roster, protected, day):
    """Monday (first decision of the matchup): swap each churnable hold for the free agent with the most games this
    week when games x value beats the hold by 15%+, then hold all week. IR moves and open-spot fills every day."""
    bd = ctx.build()
    if bd.get("error"):
        return result(notes=[f"engine: {bd['error']}"])
    ir = [p["id"] for p in bd.get("ir_moves", [])]
    fills = [f["add"] for f in bd.get("fills") or []]
    notes = [f"{p['name']} to IR" for p in bd.get("ir_moves", [])] + [f"add {f['name']}" for f in fills]
    mp = bd.get("matchup_period")
    if ctx.state.get("stacked") == mp:
        return result(ir, fills, notes=notes, stacked=mp)
    D, c = ctx.data(), ctx.context()
    sps, key = c["sps"], c["key"]
    # games x value, with value = the engine's matchup value per game (what a game is worth this week)
    def worth(p):
        return games_left(D, p, sps) * key(p)

    taken = {str(f["id"]) for f in fills}
    pool = [p for p in free_agents(D) if str(p["id"]) not in taken and games_left(D, p, sps) > 0]
    score = {p["id"]: worth(p) for p in pool}
    pool.sort(key=lambda p: -score[p["id"]])
    holds = [p for p in churnable(c["mine"], protected) if p["id"] not in set(ir)]
    holds.sort(key=worth)
    adds, drops, used = list(fills), [], set()
    for h in holds:
        hs = worth(h)
        best = next((x for x in pool if x["id"] not in used), None)
        if best is None:
            break
        bs = score[best["id"]]
        if bs > 0 and bs >= hs + STACK_EDGE * abs(hs) and bs > hs:
            used.add(best["id"])
            adds.append(best)
            drops.append(h)
            notes.append(f"add {best['name']} for {h['name']} ({games_left(D, best, sps)} games vs "
                         f"{games_left(D, h, sps)})")
    return result(ir, adds, drops, notes=notes, stacked=mp)


DECIDE = {"current": current, "fill_all": fill_all, "smart_fill": smart_fill, "two_spots": two_spots,
          "schedule_stack": schedule_stack, "hold": hold}


def decide(sid, ctx, roster, protected, day):
    """Run one strategy and enforce the protected rule on its output."""
    out = DECIDE[sid](ctx, roster, protected, day)
    bad = [i for i in out["drops"] if i in protected]
    if bad:
        out["drops"] = [i for i in out["drops"] if i not in protected]
        out["notes"] = out["notes"] + [f"refused: would drop protected player {i}" for i in bad]
        out["refused"] = bad
    return out
