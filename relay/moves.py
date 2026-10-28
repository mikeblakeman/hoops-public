"""Which roster moves the brief recommends today, from the engine output (brief_data.json) and the week plan.

Shared by tools/brief.py (the morning brief's actions) and relay/strategies.py (the strategy lab's `current`
paper team), so the lab's control makes exactly the moves Mike is told to make. Pure functions, stdlib only:
the brief must never fail because of the lab.
"""

IDLE_GAIN = 0.02


def defer_idle_swap(days):
    """The planner may schedule a swap on a day the new player adds nothing (no open slot or no game) just so he
    is rostered for the next day. Tell Mike to make that add on the day it starts paying off instead: same
    result, and the current player stays in case news changes the plan. Returns a new list of day dicts."""
    days = [dict(d) for d in days]
    if (len(days) > 1 and days[0].get("swap") and (days[0].get("day_gain") or 0) < IDLE_GAIN
            and not days[1].get("swap") and days[1].get("occupant") == days[0].get("occupant")):
        days[1].update(swap=True, drop=days[0].get("drop"), drop_id=days[0].get("drop_id"))
        days[0].update(swap=False, occupant=days[0].get("drop"), id=days[0].get("drop_id"), team=None,
                       drop=None, drop_id=None, day_gain=0.0, deferred_to=days[1]["date"])
    return days


def select_moves(bd, wp, plan="daily"):
    """Today's moves, in the order the brief lists them:
    - ir: players to move to IR (engine `ir_moves`, ESPN shows them out)
    - fills: free agents for open roster spots, no drop (engine `fills`)
    - step: today's streaming swap from the week plan (unless a fill already adds the same player)
    - top: engine add/drop swaps, used only when the week plan failed
    Applies defer_idle_swap to every stream plan in `wp` IN PLACE (the week card and ledger read the same days)."""
    wp = wp or {}
    if wp.get("stream_plan") and not wp.get("error"):
        for p in wp["stream_plan"].get("plans", {}).values():
            p["days"] = defer_idle_swap(p.get("days", []))
    wp_ok = bool(wp.get("stream_plan")) and not wp.get("error")
    plan_days = wp["stream_plan"]["plans"][plan]["days"] if wp_ok else []
    carry = (wp["stream_plan"]["plans"][plan].get("carry") or {}) if wp_ok else {}
    fills = list(bd.get("fills") or [])
    # the week plan owns the streaming spot (it sees every remaining day); engine swap moves only
    # show when there is no plan. Open-spot fills stay, they cost no drop. The plan's swap for today stands next
    # to any open-spot fill (the fill uses the spot an IR move opens, the swap uses the streaming spot), unless
    # the fill already brings in the same player.
    fill_ids = {f["add"]["id"] for f in fills}
    step = (plan_days[0] if plan_days and plan_days[0]["swap"] and plan_days[0].get("id") not in fill_ids
            else None)
    top = [] if wp_ok else [m for m in bd.get("moves", []) if m["gain"] >= 0.05][:2]
    ir = list(bd.get("ir_moves") or [])

    ir_ids = [p["id"] for p in ir]
    add_ids = [f["add"]["id"] for f in fills] + [m["add"]["id"] for m in top]
    drop_ids = [m["drop"]["id"] for m in top]
    text = [f"{p['name']} to IR" for p in ir] + [f"add {f['add']['name']}" for f in fills] + \
           [f"add {m['add']['name']} for {m['drop']['name']}" for m in top]
    if step and step.get("id"):
        add_ids.append(step["id"])
        if step.get("drop_id"):
            drop_ids.append(step["drop_id"])
        text.append(f"add {step['occupant']}" + (f" for {step['drop']}" if step.get("drop") else ""))
    return {"wp_ok": wp_ok, "plan_days": plan_days, "carry": carry, "ir": ir, "fills": fills, "step": step,
            "top": top, "ir_ids": ir_ids, "add_ids": add_ids, "drop_ids": drop_ids, "text": text}
