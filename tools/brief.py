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
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
ROOT = Path(__file__).resolve().parents[1]
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


def week_doc(wp, teams=None):
    """The brief's 'week ahead' block: coverage by day, streaming plan, trade ideas."""
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
        "stream_plan": wp["stream_plan"],
        "trades": trades,
    }


def build(snap, kind="morning", teams=None):
    """teams: optional {team_id: {"name": ..., "owners": ...}} to restore names
    (the public data mirror carries team ids only)."""
    snap = Path(snap)
    bd = json.loads((snap / "brief_data.json").read_text())
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
    mine_ids = {p["id"] for k in ("starters", "bench", "no_game", "ir") for p in lu.get(k, [])}

    actions = []
    for p in lu["starters"]:
        if p.get("inj"):
            actions.append({"kind": "watch", "text": f"Check {p['name']} before tip ({p['inj'].replace('_', ' ').lower()}).",
                            "why": "If he is ruled out, start your best bench player with a game instead."})
    for p in lu["bench"]:
        if p.get("why") == "negative":
            actions.append({"kind": "bench", "text": sentence(f"Bench {p['name']} today"),
                            "why": "His expected line costs more in close categories than it adds."})
        elif p.get("why") == "no_slot":
            actions.append({"kind": "bench", "text": f"{p['name']} has a game but no open slot.",
                            "why": "Your starters with games rate higher in this matchup."})
    for p in bd.get("ir_moves", []):
        actions.append({"kind": "ir", "text": sentence(f"Move {p['name']} to IR"), "why": "He is out; this frees a roster spot for a streamer."})
    for f in bd.get("fills", []):
        a = f["add"]
        where = (f"Then add {a['name']} ({a['team']}) to the spot that opens" if f.get("after_ir_move")
                 else f"Add {a['name']} ({a['team']}) to your open roster spot")
        today = "plays today, " if f["plays_today"] else ""
        helps = ", ".join(f["helps"]) or "several categories"
        actions.append({"kind": "add", "text": sentence(f"{where}; no drop needed"),
                        "why": f"{today}{f['games_left']} games left this matchup; helps {helps}. "
                               f"+{f['gain']:.2f} expected categories."})
    wp = bd.get("week_plan") or {}
    wp_ok = bool(wp.get("stream_plan")) and not wp.get("error")
    plan_days = wp["stream_plan"]["plans"]["low_effort"]["days"] if wp_ok else []
    # the week plan owns the streaming spot (it sees every remaining day); engine swap moves only
    # show when there is no plan. Open-spot fills stay, they cost no drop.
    step = plan_days[0] if plan_days and plan_days[0]["swap"] and not bd.get("fills") else None
    if step:
        where = (f"Add {step['occupant']} ({step['team']}), drop {step['drop']}" if step.get("drop")
                 else f"Add {step['occupant']} ({step['team']}) to your open roster spot")
        helps = ", ".join(step["helps"]) or "several categories"
        actions.append({"kind": "add", "text": sentence(where),
                        "why": f"Week plan: tips {step['tip_ct']} CT vs {step['plays_vs']}; helps {helps}. "
                               f"+{step['day_gain']:.2f} expected categories today."})
    later = [d for d in plan_days[1:] if d["swap"]]
    light = [d for d in wp.get("days", [])[1:] if len(d["my_open_slots"]) >= 4] if wp_ok else []
    if later or light:
        parts = [f"{_dow(d['date'])}: add {d['occupant']} ({d['team']})" + (f" for {d['drop']}" if d.get("drop") else "")
                 for d in later]
        thin = [f"{_dow(d['date'])} ({d['nba_games']} games, {len(d['my_open_slots'])} slots empty)" for d in light]
        actions.append({"kind": "watch",
                        "text": "Plan ahead: " + ("; ".join(parts) if parts else "no more streaming moves") + ".",
                        "why": ("Light days: " + ", ".join(thin) + ". " if thin else "") +
                               "Make each add in the morning, before the day's first tip."})
    top = [] if wp_ok else [m for m in bd["moves"] if m["gain"] >= 0.05][:2]
    for m in top:
        a, d = m["add"], m["drop"]
        today = "plays today, " if m["plays_today"] else ""
        helps = ", ".join(m["helps"]) or "several categories"
        actions.append({"kind": "add", "text": sentence(f"Add {a['name']} ({a['team']}), drop {d['name']}"),
                        "why": f"{today}{m['games_left']} games left this matchup; helps {helps}. "
                               f"+{m['gain']:.2f} expected categories."})
    if lu.get("open_slots"):
        n = lu["open_slots"]
        room = bd.get("roster_room") or {}
        has_room = bool(bd.get("fills") or room.get("empty") or room.get("via_ir"))
        why = ("Fill them with a free agent who plays today if a move above does not." if has_room or top or step else
               "Your roster is full and the model finds no add worth a drop, so no move is needed.")
        actions.append({"kind": "watch", "text": f"{n} lineup spot{'s' if n != 1 else ''} empty today "
                                                 f"(only {len(lu.get('starters', []))} of your players have games).",
                        "why": why})

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
        "moves": [as_move(f) for f in bd.get("fills", [])] + ([plan_move(step)] if step else (
            [] if wp_ok else bd["moves"])),
        "alternatives": [as_move(f) for f in bd.get("fill_alternatives", [])] + (
            [plan_move(step, a) for a in step["alternatives"]] if step else ([] if wp_ok else bd["alternatives"])),
        "roster_room": bd.get("roster_room"),
        "week": week_doc(wp, teams) if wp_ok else None,
        "opp_injuries": bd.get("opp_injuries", []),
        "injury_notes": [{"name": inj[i]["name"], "status": inj[i]["status"], "comment": inj[i]["comment"]}
                         for i in mine_ids if i in inj],
        "news": [],
        "experts": [],
        "prelock": None,
        "weekly": None,
        "notes": [],
        "data_status": {"ok": status.get("ok"), "warnings": status.get("warnings", [])[:5]},
    }
    return doc, bd


def log_decisions(bd, kind):
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
    for recs, rec in ((bd.get("fills", []), "add"), (bd.get("fill_alternatives", []), "pass")):
        for f in recs:
            new.append({"rec": rec, "player_id": f["add"]["id"], "name": f["add"]["name"], "team": f["add"]["team"],
                        "related_id": "", "related_name": "open spot", "gain": f["gain"]})
    wp = bd.get("week_plan") or {}
    if wp.get("stream_plan") and not wp.get("error") and not bd.get("fills"):
        d0 = wp["stream_plan"]["plans"]["low_effort"]["days"][0]
        if d0["swap"]:
            new.append({"rec": "add", "player_id": d0["id"], "name": d0["occupant"], "team": d0["team"],
                        "related_id": d0.get("drop_id") or "", "related_name": d0.get("drop") or "open spot",
                        "gain": d0["day_gain"], "note": "week_plan"})
            for a in d0["alternatives"]:
                new.append({"rec": "pass", "player_id": a["id"], "name": a["name"], "team": a["team"],
                            "related_id": d0.get("drop_id") or "", "related_name": d0.get("drop") or "open spot",
                            "gain": a["gain"], "note": "week_plan"})
    moves_to_log = [] if wp.get("stream_plan") and not wp.get("error") else bd["moves"]
    alts_to_log = [] if wp.get("stream_plan") and not wp.get("error") else bd["alternatives"]
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
        "date": day, "matchup_period": mp,
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
    ap.add_argument("--teams", help="JSON file {team_id: {name, owners}} to restore team names")
    a = ap.parse_args()
    teams = json.loads(Path(a.teams).read_text()) if a.teams else None
    doc, bd = build(a.snap, a.kind, teams)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(doc, indent=1))
    n = 0 if a.no_log else log_decisions(bd, a.kind)
    print(json.dumps({"out": a.out, "actions": len(doc["actions"]), "logged": n,
                      "expected_cats": bd["expected_cats"], "opponent": bd["opponent"]["name"]}))


if __name__ == "__main__":
    main()
