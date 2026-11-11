"""Which roster moves the brief recommends today, from the engine output (brief_data.json) and the week plan.

Shared by tools/brief.py (the morning brief's actions) and relay/strategies.py (the strategy lab's `current`
paper team), so the lab's control makes exactly the moves Mike is told to make. Pure functions, stdlib only:
the brief must never fail because of the lab.
"""

import json
from pathlib import Path

IDLE_GAIN = 0.02
STRATEGY_FILE = Path(__file__).with_name("strategy.json")
DEFAULT_STRATEGY = "current"


def load_strategy(path=None):
    """relay/strategy.json: which strategy the brief's actions follow (Mike approves every switch; see
    claude/engine/strategy-lab-spec.md section 6). Returns {active, since, approved_by, previous, control}.
    `control` is what the lab measures against: the previous strategy after a switch, else the active one.
    A missing or broken file means `current`, the week planner's one streaming spot."""
    try:
        d = json.loads(Path(path or STRATEGY_FILE).read_text())
    except (OSError, ValueError):
        d = {}
    active = d.get("active") or DEFAULT_STRATEGY
    prev = d.get("previous") or None
    return {"active": active, "since": d.get("since"), "approved_by": d.get("approved_by"), "previous": prev,
            "control": prev if prev and prev != active else active}


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


def activation_moves(bd):
    """Today's IR activations from the engine output (brief_data `ir_activations`): a healthy player in an IR slot
    makes ESPN refuse every add, so he comes off IR before anything else (claude/engine/ir-activation-spec.md).
    Returns {activations, activate_ids, drop_ids, blocked, text}. `blocked`: an activation needs Mike to choose
    the drop (every option is protected), so no add can be made today."""
    acts = list(bd.get("ir_activations") or [])
    done = [a for a in acts if not a.get("blocked")]
    text = []
    for a in acts:
        if a.get("blocked"):
            text.append(f"choose a drop to move {a['player']['name']} off IR")
        else:
            text.append(f"{a['player']['name']} off IR" + (f" for {a['drop']['name']}" if a.get("drop") else ""))
    return {"activations": acts, "activate_ids": [str(a["player"]["id"]) for a in done],
            "drop_ids": [str(a["drop"]["id"]) for a in done if a.get("drop")],
            "blocked": any(a.get("blocked") for a in acts), "text": text}


def select_moves(bd, wp, plan="daily"):
    """Today's moves, in the order the brief lists them (ESPN's order):
    - activate: healthy players to move off IR, each with the drop that makes room (engine `ir_activations`).
      ESPN blocks every add until this is done; if a drop needs Mike's choice, no add is made today.
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
    act = activation_moves(bd)
    if act["blocked"] or bd.get("adds_blocked"):
        fills, step, top = [], None, []
    # the stream step can never drop a player the activation already dropped
    if step and step.get("drop_id") and str(step["drop_id"]) in act["drop_ids"]:
        step = None

    ir_ids = [p["id"] for p in ir]
    add_ids = [f["add"]["id"] for f in fills] + [m["add"]["id"] for m in top]
    drop_ids = list(act["drop_ids"]) + [m["drop"]["id"] for m in top]
    text = act["text"] + [f"{p['name']} to IR" for p in ir] + [f"add {f['add']['name']}" for f in fills] + \
        [f"add {m['add']['name']} for {m['drop']['name']}" for m in top]
    if step and step.get("id"):
        add_ids.append(step["id"])
        if step.get("drop_id"):
            drop_ids.append(step["drop_id"])
        text.append(f"add {step['occupant']}" + (f" for {step['drop']}" if step.get("drop") else ""))
    return {"wp_ok": wp_ok, "plan_days": plan_days, "carry": carry, "ir": ir, "fills": fills, "step": step,
            "top": top, "ir_ids": ir_ids, "add_ids": add_ids, "drop_ids": drop_ids, "text": text,
            "activations": act["activations"], "activate_ids": act["activate_ids"],
            "activate_drop_ids": act["drop_ids"], "adds_blocked": act["blocked"]}
