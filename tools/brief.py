"""Turn the engine's brief_data.json into the brief document the page renders,
and log today's recommendations plus the alternatives to the decision ledger.

Usage:
  python tools/brief.py snapshots out/brief.json [--kind morning|prelock] [--no-log]

Claude then edits only the judgment fields (headline, summary, news, experts,
notes) before writing the document to the page's database as brief/latest.
"""
import argparse
import csv
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from relay.moves import IDLE_GAIN, defer_idle_swap, load_strategy, select_moves  # noqa: E402,F401  (shared with the lab)

LEDGER = ROOT / "ledger"
DEC_FIELDS = ["date", "kind", "matchup_period", "rec", "player_id", "name", "team",
              "related_id", "related_name", "gain", "note"]
# the brief page renders every pickup as "Add X ... Drop Y"; a fill drops no one
OPEN_SPOT = {"id": "", "name": "no one (open spot)", "team": "", "pos": "", "inj": "", "line": {}, "helps": []}


def as_move(f):
    return dict(f, drop=OPEN_SPOT)


def plan_move(d, add=None):
    """A week-plan step (or one of its alternatives) as the page's move card."""
    a = add or d
    drop = ({"id": d.get("drop_id") or "", "name": d["drop"], "team": "", "pos": "", "inj": "", "line": {}, "helps": []}
            if d.get("drop") else OPEN_SPOT)
    return {"add": {"id": a["id"], "name": a.get("occupant") or a.get("name"), "team": a["team"], "pos": a["pos"],
                    "inj": "", "rank": a.get("rank"), "line": a.get("line", {}), "helps": a.get("helps", [])},
            "gain": a.get("day_gain", a.get("gain")), "games_left": a.get("games_left", 0),
            "plays_today": True, "helps": a.get("helps", []), "drop": drop}


def rows(p):
    p = Path(p)
    if not p.exists() or p.stat().st_size == 0:
        return []
    with p.open() as f:
        return list(csv.DictReader(f))


def sentence(t):
    return t if t.endswith(".") else t + "."


def pct(x):
    return f"{x:.3f}".lstrip("0") if isinstance(x, (int, float)) else "–"


def _dow(d):
    return datetime.strptime(d, "%Y-%m-%d").strftime("%a %-m/%-d")


def yesterday_doc(snap):
    """Last finished day: actual vs projected. Never breaks the brief."""
    try:
        import sys
        sys.path.insert(0, str(ROOT))
        from relay import review
        return review.yesterday(snap)
    except Exception:  # noqa: BLE001
        return None


def model_check(snap):
    """How far the model has been off, and whether any correction is applied yet."""
    try:
        d = json.loads((Path(snap) / "calibration.json").read_text())
    except (OSError, ValueError):
        return None
    return {k: d.get(k) for k in ("through", "days", "player_games", "min_games_needed", "active", "raw_ratio", "factors")}


PLANS = ("daily", "low_effort")


def week_doc(wp, teams=None, plan="daily"):
    """The brief's 'week ahead' block: coverage by day, streaming plan, trade ideas.
    `plan_used` names the stream plan the actions follow; the page's week card draws that same plan."""
    teams = teams or {}
    trades = []
    for t in (wp.get("trades") or {}).get("top", [])[:5]:
        pid = str(t["partner_team_id"])
        trades.append(dict(t, partner=(teams.get(pid) or {}).get("name") or f"Team {pid}"))
    return {
        "coverage": [{k: d[k] for k in ("date", "nba_games", "my_starters", "my_open_slots", "my_no_slot",
                                         "opp_starters", "opp_open_slots")} for d in wp.get("days", [])],
        "light_days": wp.get("light_days", []),
        "expected_cats": wp.get("expected_cats_now"),
        "banked_past_days_at_projection": wp.get("banked_past_days_at_projection"),
        "plan_used": plan,
        "stream_plan": wp["stream_plan"],
        "trades": trades,
    }


def fresh_week_plan(snap, bd):
    """The data run can be a day old (a morning run that did not land). Its week plan then starts on a day that is
    already played, so rebuild the plan from the same snapshots for today. Falls back to the stored plan."""
    today = datetime.now(CT).date().isoformat()
    if not bd.get("date") or bd["date"] >= today:
        return bd.get("week_plan") or {}, None
    try:
        import sys
        sys.path.insert(0, str(ROOT))
        from relay import weekplan
        wp = weekplan.plan(snap, today)
        if wp.get("stream_plan") and not wp.get("error"):
            return wp, today
    except Exception:  # noqa: BLE001
        pass
    return bd.get("week_plan") or {}, None


def lineup_after(snap, day, add_ids, drop_ids, ir_ids, bench_ids=()):
    """Today's lineup with the recommended moves applied (relay.matchup.lineup_after); None if it fails."""
    try:
        import sys
        sys.path.insert(0, str(ROOT))
        from datetime import date as _date
        from relay import matchup
        return matchup.lineup_after(snap, _date.fromisoformat(day), add_ids, drop_ids, ir_ids, bench_ids)
    except Exception:  # noqa: BLE001
        return None


def coverage_after(snap, day, add_ids, drop_ids, ir_ids, current_streamer, streamer_by_date):
    """relay.matchup.coverage_after for the brief's date; None if it fails (the planner's rows stay)."""
    try:
        from datetime import date as _date
        from relay import matchup
        return matchup.coverage_after(snap, _date.fromisoformat(day), add_ids, drop_ids, ir_ids,
                                      current_streamer, streamer_by_date)
    except Exception:  # noqa: BLE001
        return None


def apply_coverage(wp, cov):
    """Overwrite my side of the week plan's coverage rows with `cov` (matched by date)."""
    by = {c["date"]: c for c in cov or []}
    for d in wp.get("days", []):
        c = by.get(d.get("date"))
        if c:
            d.update(my_starters=c["my_starters"], my_open_slots=c["my_open_slots"],
                     my_no_slot=c["my_no_slot"], starters=c["starters"], plan_roster=True)
    wp["light_days"] = [d["date"] for d in wp.get("days", []) if len(d.get("my_open_slots", [])) >= 4]


def _drop_past_days(wp, today):
    wp = dict(wp, days=[d for d in wp.get("days", []) if d.get("date", "") >= today])
    sp = dict(wp["stream_plan"])
    sp["plans"] = {k: dict(p, days=[d for d in p.get("days", []) if d.get("date", "") >= today])
                   for k, p in sp.get("plans", {}).items()}
    wp["stream_plan"] = sp
    return wp


def fresh_brief_data(snap, bd):
    """The newest data run can be from an earlier day (GitHub starts scheduled jobs hours late). Every section of
    the brief must describe the same day, so rebuild the whole matchup output (lineup, odds, moves, week plan)
    for today from the same snapshots. Returns (brief_data, date it was originally built for or None)."""
    today = datetime.now(CT).date()
    if not bd.get("date") or bd["date"] >= today.isoformat():
        return bd, None
    try:
        import sys
        sys.path.insert(0, str(ROOT))
        from relay import matchup
        nb = matchup.build(snap, today)
        if nb.get("lineup") and not nb.get("error"):
            old = bd.get("week_plan") or {}
            if (nb.get("week_plan") or {}).get("error") and old.get("stream_plan") and not old.get("error"):
                # planner failed on the rebuild: keep the stored plan, minus the days already played
                nb["week_plan"], nb["week_plan_date"] = _drop_past_days(old, today.isoformat()), bd["date"]
            return nb, bd["date"]
    except Exception:  # noqa: BLE001
        pass
    return bd, None


def ir_why(p, row=None):
    """IR moves depend on ESPN's live designation, which can change after the data run (Kyrie, Oct 25 2026: listed
    Out after a rest night, day-to-day by morning). Say so, and flag short rest designations that usually clear."""
    why = ("He is listed out; this frees a roster spot for a streamer. ESPN allows IR only while he shows Out. "
           "If ESPN shows him day-to-day, skip this and the add that depends on it.")
    row = row or {}
    back = row.get("return_date") or ""
    tomorrow = (datetime.now(CT).date() + timedelta(days=1)).isoformat()
    if "rest" in (row.get("comment") or "").lower() or (back and back <= tomorrow):
        why = (f"Short-term designation{f' (back about {back})' if back else ''}; these often switch to day-to-day "
               f"before lock, so check ESPN first. ") + why
    return why


def carry_note(step, carry):
    """Why a streaming add also looks at next week. Only for adds the carry check put into the plan."""
    if not step.get("carry") or not carry.get("changed"):
        return ""
    return (f" Also sets up next week: +{carry['next_week_gain']:.2f} next week for "
            f"{carry['this_week_cost']:.2f} this week (limit {carry['max_cost']:.2f}).")


def next_opp_doc(wp, teams=None, plan="daily"):
    """The next-opponent card: shown only in the last days of a matchup (the planner decides the window).
    Its streaming note comes from the same plan the actions follow, so the two can never disagree."""
    n = wp.get("next_opponent")
    if not n or n.get("error"):
        return None
    teams = teams or {}
    tid = str(n.get("opponent_team_id"))
    t = teams.get(tid) or {}
    rec = ((n.get("form") or {}).get("this_week") or {}).get("record")
    if rec:
        rec = dict(rec, vs=(teams.get(str(rec.get("vs_team_id"))) or {}).get("name") or f"Team {rec.get('vs_team_id')}")
        n = dict(n, form=dict(n["form"], this_week=dict(n["form"]["this_week"], record=rec)))
    carry = (wp["stream_plan"]["plans"].get(plan) or {}).get("carry")
    return dict(n, name=t.get("name") or f"Team {tid}", owners=t.get("owners"), carry=carry, plan_used=plan)


def lab_doc(day, teams=None, ledger=None):
    """The Strategy lab card (ledger/lab/summary.json, written by relay/lab.py in the data job). Team names are
    restored from --teams; paper moves show only when they were decided for this brief's day. None if missing."""
    try:
        lab = json.loads(((ledger or LEDGER) / "lab" / "summary.json").read_text())
    except (OSError, ValueError):
        return None
    if not lab.get("strategies"):
        return None
    wk = dict(lab.get("week") or {})
    t = (teams or {}).get(str(wk.get("opponent_team_id")))
    if t:
        wk["opponent"] = t.get("name")
    lab["week"] = wk
    if lab.get("decided") != day:
        lab["strategies"] = [dict(s, today=[]) for s in lab["strategies"]]
    return lab


def active_decision(snap, day, sid):
    """Today's moves for the real roster under the active strategy (relay.strategies.real_decision), or None if it
    fails; the brief then falls back to the current plan and says so."""
    try:
        from datetime import date as _date
        from relay import strategies as S
        if sid not in S.DECIDE:
            return None
        return S.real_decision(snap, _date.fromisoformat(day), sid)
    except Exception:  # noqa: BLE001
        return None


def strategy_name(sid):
    try:
        from relay import strategies as S
        return S.INFO[sid][0]
    except Exception:  # noqa: BLE001
        return sid


def strategy_actions(dec, inj, cfg):
    """Actions for the moves an active (non-current) strategy makes today."""
    who = f"{strategy_name(dec['strategy'])} strategy" + (f" (approved {cfg['since']})" if cfg.get("since") else "")
    acts = [{"kind": "ir", "text": sentence(f"Move {p['name']} to IR"), "why": ir_why(p, inj.get(p["id"]))}
            for p in dec.get("ir_players", []) if p]
    for m in dec.get("moves", []):
        a, d = m["add"], m.get("drop")
        text = (f"Add {a['name']} ({a['team']}), drop {d['name']}" if d
                else f"Add {a['name']} ({a['team']}) to your open roster spot; no drop needed")
        acts.append({"kind": "add", "text": sentence(text), "why": f"{who}."})
    return acts


def strategy_text(dec):
    return ([f"{p['name']} to IR" for p in dec.get("ir_players", []) if p] +
            [f"add {m['add']['name']}" + (f" for {m['drop']['name']}" if m.get("drop") else "")
             for m in dec.get("moves", [])])


def switch_actions(lab):
    """Top actions from the Strategy lab: a strategy is Ready, or the active one should be switched back.
    Nothing changes until Mike replies in chat (claude/engine/strategy-lab-spec.md section 6)."""
    out = []
    sb = (lab or {}).get("switch_back")
    if sb:
        out.append({"kind": "watch", "text": f"Switch back to {strategy_name(sb['id'])}?",
                    "why": f"{sb['summary']} Reply 'switch to {sb['id']}' to approve. Nothing changes until you do."})
    r = (lab or {}).get("ready")
    if r and r.get("edge") is not None:
        out.append({"kind": "watch",
                    "text": f"Ready to switch to {r.get('name') or strategy_name(r['id'])}: {r['edge']:+.1f} more "
                            f"categories a week over {r['weeks']} weeks ({r['live_weeks']} live), "
                            f"{round((r.get('confidence') or 0) * 100)}% sure.",
                    "why": f"Reply 'switch to {r['id']}' to approve. Nothing changes until you do."})
    return out


def stale_note(status, hours=10, rebuilt_from=None, lineup_date=None):
    """A visible warning when the data run behind the brief is old, saying exactly what that means for this brief."""
    try:
        run = datetime.fromisoformat(status["run_ct"])
    except (KeyError, TypeError, ValueError):
        return []
    age = (datetime.now(CT) - run).total_seconds() / 3600
    today = datetime.now(CT).date().isoformat()
    if age < hours and not rebuilt_from and (not lineup_date or lineup_date >= today):
        return []
    when = f"{run.strftime('%a %-I:%M %p')} CT"
    if rebuilt_from:
        return [f"Data warning: the newest data is from {when} ({age:.0f} hours old). Every section was rebuilt for "
                "today from it, but games and roster moves since then (including any you made last night) are not "
                "counted yet. Check ESPN before acting."]
    if lineup_date and lineup_date < today:
        return [f"Data warning: the newest data is from {when} and could not be rebuilt for today, so the lineup "
                f"below is for {_dow(lineup_date)}. Check ESPN before acting."]
    return [f"Data warning: the last data run was {age:.0f} hours ago ({when}), so rosters, scores and injuries may "
            "be behind."]


def build(snap, kind="morning", teams=None, plan="daily"):
    """teams: optional {team_id: {"name": ..., "owners": ...}} to restore names
    (the public data mirror carries team ids only). plan: which stream plan the actions and the week card
    follow ("low_effort" = few adds and hold, "daily" = chase every gain)."""
    if plan not in PLANS:
        raise ValueError(f"plan must be one of {PLANS}")
    snap = Path(snap)
    bd = json.loads((snap / "brief_data.json").read_text())
    bd, rebuilt_from = fresh_brief_data(snap, bd)
    if teams:
        for side in ("me", "opponent"):
            t = teams.get(str(bd.get(side, {}).get("team_id")))
            if t:
                bd[side]["name"] = t.get("name")
                if side == "opponent":
                    bd[side]["owners"] = t.get("owners")
    status = json.loads((snap / "status.json").read_text())
    inj = {r["player_id"]: r for r in rows(snap / "injuries.csv")}
    lu = bd["lineup"]

    actions = []
    for p in bd.get("ir_moves", []):
        actions.append({"kind": "ir", "text": sentence(f"Move {p['name']} to IR"), "why": ir_why(p, inj.get(p["id"]))})
    for f in bd.get("fills", []):
        a = f["add"]
        where = (f"Then add {a['name']} ({a['team']}) to the spot that opens, only if the IR move went through"
                 if f.get("after_ir_move") else f"Add {a['name']} ({a['team']}) to your open roster spot")
        today = "plays today, " if f["plays_today"] else ""
        helps = ", ".join(f["helps"]) or "several categories"
        actions.append({"kind": "add", "text": sentence(f"{where}; no drop needed"),
                        "why": f"{today}{f['games_left']} games left this matchup; helps {helps}. "
                               f"+{f['gain']:.2f} expected categories."})
    wp, replanned = fresh_week_plan(snap, bd)
    # which moves to make today: shared with the strategy lab's control (relay/moves.py), so the lab's `current`
    # paper team makes exactly these moves. It also applies defer_idle_swap to every stream plan in wp.
    sel = select_moves(bd, wp, plan)
    bd["week_plan"] = wp   # the ledger logs the same plan the actions follow
    wp_ok, plan_days, carry, step, top = sel["wp_ok"], sel["plan_days"], sel["carry"], sel["step"], sel["top"]
    # Phase 3: relay/strategy.json names the strategy Mike approved. `current` is the logic above; any other
    # strategy decides today's moves itself (relay.strategies.real_decision) and replaces the actions so far.
    cfg = load_strategy()
    strat, strat_note = None, None
    if cfg["active"] != "current":
        strat = active_decision(snap, bd["date"], cfg["active"])
        if strat is None:
            strat_note = (f"The active strategy ({strategy_name(cfg['active'])}) could not run today, so the actions "
                          "follow the current plan.")
        else:
            actions = strategy_actions(strat, inj, cfg)
            step, top, carry, plan_days = None, [], {}, []
            bd["strategy_moves"] = strat      # the ledger logs these moves
            if wp_ok:
                cov = coverage_after(snap, bd["date"], strat["adds"], strat["drops"], strat["ir"], None, None)
                apply_coverage(wp, cov)
    if step:
        where = (f"Add {step['occupant']} ({step['team']}), drop {step['drop']}" if step.get("drop")
                 else f"Add {step['occupant']} ({step['team']}) to your open roster spot")
        helps = ", ".join(step["helps"]) or "several categories"
        actions.append({"kind": "add", "text": sentence(where),
                        "why": f"Week plan: tips {step['tip_ct']} CT vs {step['plays_vs']}; helps {helps}. "
                               f"+{step['day_gain']:.2f} expected categories today." + carry_note(step, carry)})
    if wp_ok and plan_days and not strat:
        # Week ahead coverage on the roster the brief recommends (today's IR moves and fills, the streaming
        # spot's planned player each day), so its "You start" column agrees with the lineup card and the plan.
        d0 = plan_days[0]
        cur_id = d0.get("drop_id") if d0.get("swap") else d0.get("id")
        by_date = {d["date"]: d.get("id") for d in plan_days}
        if d0.get("swap") and not step:          # today's plan swap is not one of today's actions
            by_date[d0["date"]] = cur_id
        cov = coverage_after(snap, bd["date"], [f["add"]["id"] for f in bd.get("fills", [])], [],
                             [p["id"] for p in bd.get("ir_moves", [])], cur_id, by_date)
        apply_coverage(wp, cov)
    later = [d for d in plan_days[1:] if d["swap"]]
    light = [d for d in wp.get("days", [])[1:] if len(d["my_open_slots"]) >= 4] if wp_ok else []
    if later or light:
        parts = [f"{_dow(d['date'])}: add {d['occupant']} ({d['team']})" + (f" for {d['drop']}" if d.get("drop") else "")
                 + (" (also sets up next week)" if d.get("carry") else "") for d in later]
        thin = [f"{_dow(d['date'])} ({d['nba_games']} games, {len(d['my_open_slots'])} slots empty)" for d in light]
        actions.append({"kind": "watch",
                        "text": "Plan ahead: " + ("; ".join(parts) if parts else "no more streaming moves") + ".",
                        "why": ("Light days: " + ", ".join(thin) + ". " if thin else "") +
                               "Make each add in the morning, before the day's first tip."})
    for m in top:
        a, d = m["add"], m["drop"]
        today = "plays today, " if m["plays_today"] else ""
        helps = ", ".join(m["helps"]) or "several categories"
        actions.append({"kind": "add", "text": sentence(f"Add {a['name']} ({a['team']}), drop {d['name']}"),
                        "why": f"{today}{m['games_left']} games left this matchup; helps {helps}. "
                               f"+{m['gain']:.2f} expected categories."})
    # The lineup card shows today's lineup AFTER the moves above, so it names the same players as the actions.
    ir_ids, add_ids, drop_ids, moves_txt = sel["ir_ids"], sel["add_ids"], sel["drop_ids"], sel["text"]
    bench_ids = []
    if strat:
        ir_ids, add_ids, drop_ids = strat["ir"], strat["adds"], strat["drops"]
        moves_txt, bench_ids = strategy_text(strat), strat["bench"]
    if add_ids or drop_ids or ir_ids or bench_ids:
        after = lineup_after(snap, bd["date"], add_ids, drop_ids, ir_ids, bench_ids)
        if after:
            lu = dict(after, after_moves=moves_txt)
            bd["lineup"] = lu   # the ledger logs the lineup the brief shows
        else:
            lu = dict(lu, after_moves=None)

    first = []
    for p in lu["starters"]:
        if p.get("inj"):
            first.append({"kind": "watch", "text": f"Check {p['name']} before tip ({p['inj'].replace('_', ' ').lower()}).",
                          "why": "If he is ruled out, start your best bench player with a game instead."})
    for p in lu["bench"]:
        if p.get("why") == "negative":
            first.append({"kind": "bench", "text": sentence(f"Bench {p['name']} today"),
                          "why": "His expected line costs more in close categories than it adds."})
        elif p.get("why") == "no_slot":
            first.append({"kind": "bench", "text": f"{p['name']} has a game but no open slot.",
                          "why": "Your starters with games rate higher in this matchup."})
    lab = lab_doc(bd["date"], teams)
    actions = switch_actions(lab) + first + actions
    if lu.get("open_slots"):
        n = lu["open_slots"]
        room = bd.get("roster_room") or {}
        room_left = int(room.get("empty") or 0) + len(room.get("via_ir") or []) - len(bd.get("fills") or [])
        if strat:
            room_left = int(room.get("empty") or 0) + len(strat["ir"]) - (len(strat["adds"]) - len(strat["drops"]))
        if room_left > 0:
            why = "You have an open roster spot: fill it with a free agent who plays today."
        elif step or top or (strat and strat["adds"]):
            why = ("Your roster is full, so filling them means dropping someone. The add above is the only drop "
                   "worth it today; the lineup below already includes it.")
        else:
            why = "Your roster is full and no add is worth a drop today, so leave them open."
        actions.append({"kind": "watch", "text": f"{n} lineup spot{'s' if n != 1 else ''} empty today "
                                                 f"(only {len(lu.get('starters', []))} of your players have games).",
                        "why": why})
    mine_ids = {p["id"] for k in ("starters", "bench", "no_game", "ir") for p in lu.get(k, [])}
    # the Week ahead card shows the stream plan only when the brief follows it (`current`)
    wp_doc = dict(wp, stream_plan=dict(wp["stream_plan"], plans={}, second_spot=None)) if (strat and wp_ok) else wp

    doc = {
        "date": bd["date"],
        "kind": kind,
        "generated_ct": datetime.now(CT).isoformat(timespec="minutes"),
        "data_run_ct": status.get("run_ct"),
        "headline": "",
        "summary": "",
        "matchup": {k: bd[k] for k in ("matchup_period", "period", "in_progress", "me", "opponent",
                                       "expected_cats", "player_games_left", "categories")},
        "actions": actions,
        "lineup": lu,
        "moves": [] if strat else [as_move(f) for f in bd.get("fills", [])] + ([plan_move(step)] if step else (
            [] if wp_ok else bd["moves"])),
        "alternatives": [] if strat else [as_move(f) for f in bd.get("fill_alternatives", [])] + (
            [plan_move(step, a) for a in step["alternatives"]] if step else ([] if wp_ok else bd["alternatives"])),
        "roster_room": bd.get("roster_room"),
        "week": dict(week_doc(wp_doc, teams, plan), plan_date=replanned or bd.get("week_plan_date") or bd["date"],
                     strategy=cfg["active"]) if wp_ok else None,
        "next_opponent": next_opp_doc(wp_doc, teams, plan) if wp_ok else None,
        "strategy": {"active": cfg["active"], "name": strategy_name(cfg["active"]), "since": cfg["since"],
                     "previous": cfg["previous"], "followed": cfg["active"] if (strat or cfg["active"] == "current")
                     else "current"},
        "yesterday": yesterday_doc(snap),
        "model_check": model_check(snap),
        "opp_injuries": bd.get("opp_injuries", []),
        "injury_notes": [{"name": inj[i]["name"], "status": inj[i]["status"], "comment": inj[i]["comment"]}
                         for i in mine_ids if i in inj],
        "news": [],
        "experts": [],
        "prelock": None,
        "weekly": None,
        "notes": stale_note(status, rebuilt_from=rebuilt_from, lineup_date=lu.get("date")) +
                 ([strat_note] if strat_note else []) +
                 ([f"Moves follow the {strategy_name(cfg['active'])} strategy" +
                   (f", approved {cfg['since']}." if cfg.get("since") else ".")] if strat else []),
        "data_status": {"ok": status.get("ok"), "warnings": status.get("warnings", [])[:5]},
    }
    doc["matchup"]["trend"] = trend_doc(bd)
    doc["lab"] = lab
    return doc, bd


def trend_doc(bd, ledger=None):
    """Win chance per category, one point per day of this matchup, for the page's trend lines.
    Past days come from ledger/context/<date>.json (the last data run of that day, written before
    the day's games); today comes from the fresh build. [] when there is nothing to draw."""
    ctx_dir = (ledger or LEDGER) / "context"
    mp, start, today = bd["matchup_period"], bd["period"]["start"], bd["date"]
    pts = []
    for fp in sorted(ctx_dir.glob("*.json")) if ctx_dir.exists() else []:
        day = fp.stem
        if day < start or day >= today:
            continue
        try:
            d = json.loads(fp.read_text())
        except (OSError, ValueError):
            continue
        if d.get("matchup_period") != mp:
            continue
        p = {c: v["p"] for c, v in (d.get("projected") or {}).items()}
        if p:
            exp = d.get("expected_cats")
            pts.append({"date": day, "expected_cats": round(exp if exp is not None else sum(p.values()), 2), "p": p})
    pts.append({"date": today, "expected_cats": bd["expected_cats"],
                "p": {c["cat"]: c["p"] for c in bd["categories"]}})
    return pts if len(pts) >= 2 else []


def log_decisions(bd, kind, plan="daily"):
    LEDGER.mkdir(exist_ok=True)
    path = LEDGER / "decisions.csv"
    existing = rows(path)
    day = bd["date"]
    # one log per day and kind: replace earlier rows for the same day/kind
    keep = [r for r in existing if not (r["date"] == day and r["kind"] == kind)]
    mp = bd["matchup_period"]
    new = []
    for p in bd["lineup"]["starters"]:
        new.append({"rec": "start", "player_id": p["id"], "name": p["name"], "team": p["team"]})
    for p in bd["lineup"]["bench"]:
        new.append({"rec": "bench", "player_id": p["id"], "name": p["name"], "team": p["team"], "note": p.get("why", "")})
    fill_recs = () if bd.get("strategy_moves") else ((bd.get("fills", []), "add"), (bd.get("fill_alternatives", []), "pass"))
    for recs, rec in fill_recs:
        for f in recs:
            new.append({"rec": rec, "player_id": f["add"]["id"], "name": f["add"]["name"], "team": f["add"]["team"],
                        "related_id": "", "related_name": "open spot", "gain": f["gain"]})
    wp = bd.get("week_plan") or {}
    strat = bd.get("strategy_moves")
    for m in (strat or {}).get("moves", []):
        d = m.get("drop") or {}
        new.append({"rec": "add", "player_id": m["add"]["id"], "name": m["add"]["name"], "team": m["add"]["team"],
                    "related_id": d.get("id", ""), "related_name": d.get("name") or "open spot",
                    "note": f"strategy:{strat['strategy']}"})
    if not strat and wp.get("stream_plan") and not wp.get("error") and wp["stream_plan"]["plans"][plan].get("days"):
        d0 = wp["stream_plan"]["plans"][plan]["days"][0]
        if d0["swap"] and d0.get("id") not in {f["add"]["id"] for f in bd.get("fills", [])}:   # same rule as build()
            new.append({"rec": "add", "player_id": d0["id"], "name": d0["occupant"], "team": d0["team"],
                        "related_id": d0.get("drop_id") or "", "related_name": d0.get("drop") or "open spot",
                        "gain": d0["day_gain"], "note": "week_plan"})
            for a in d0["alternatives"]:
                new.append({"rec": "pass", "player_id": a["id"], "name": a["name"], "team": a["team"],
                            "related_id": d0.get("drop_id") or "", "related_name": d0.get("drop") or "open spot",
                            "gain": a["gain"], "note": "week_plan"})
    moves_to_log = [] if strat or (wp.get("stream_plan") and not wp.get("error")) else bd["moves"]
    alts_to_log = [] if strat or (wp.get("stream_plan") and not wp.get("error")) else bd["alternatives"]
    for m in moves_to_log:
        new.append({"rec": "add", "player_id": m["add"]["id"], "name": m["add"]["name"], "team": m["add"]["team"],
                    "related_id": m["drop"]["id"], "related_name": m["drop"]["name"], "gain": m["gain"]})
    for m in alts_to_log:
        new.append({"rec": "pass", "player_id": m["add"]["id"], "name": m["add"]["name"], "team": m["add"]["team"],
                    "related_id": m["drop"]["id"], "related_name": m["drop"]["name"], "gain": m["gain"]})
    for r in new:
        r.update({"date": day, "kind": kind, "matchup_period": mp})
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=DEC_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(keep + new)
    ctx = LEDGER / "context"
    ctx.mkdir(exist_ok=True)
    (ctx / f"{day}.json").write_text(json.dumps({
        "date": day, "matchup_period": mp, "expected_cats": bd["expected_cats"],
        "weights": {c["cat"]: c["weight"] for c in bd["categories"]},
        "projected": {c["cat"]: {"mine": c["mine"], "theirs": c["theirs"], "p": c["p"]} for c in bd["categories"]},
        "attempts": {c["cat"]: c.get("att_mine") for c in bd["categories"] if c.get("att_mine")},
    }, indent=1))
    return len(new)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("snap")
    ap.add_argument("out")
    ap.add_argument("--kind", default="morning")
    ap.add_argument("--no-log", action="store_true")
    ap.add_argument("--plan", choices=PLANS, default="daily",
                    help="stream plan the actions and the week card follow (one plan, never a mix)")
    ap.add_argument("--teams", help="JSON file {team_id: {name, owners}} to restore team names")
    a = ap.parse_args()
    teams = json.loads(Path(a.teams).read_text()) if a.teams else None
    doc, bd = build(a.snap, a.kind, teams, a.plan)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(doc, indent=1))
    n = 0 if a.no_log else log_decisions(bd, a.kind, a.plan)
    print(json.dumps({"out": a.out, "actions": len(doc["actions"]), "logged": n,
                      "expected_cats": bd["expected_cats"], "opponent": bd["opponent"]["name"]}))


if __name__ == "__main__":
    main()
