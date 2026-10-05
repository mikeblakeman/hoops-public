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
    top = [m for m in bd["moves"] if m["gain"] >= 0.05][:2]
    for m in top:
        a, d = m["add"], m["drop"]
        today = "plays today, " if m["plays_today"] else ""
        helps = ", ".join(m["helps"]) or "several categories"
        actions.append({"kind": "add", "text": sentence(f"Add {a['name']} ({a['team']}), drop {d['name']}"),
                        "why": f"{today}{m['games_left']} games left this matchup; helps {helps}. "
                               f"+{m['gain']:.2f} expected categories."})
    if lu.get("open_slots"):
        n = lu["open_slots"]
        actions.append({"kind": "watch", "text": f"{n} lineup spot{'s' if n != 1 else ''} empty today.",
                        "why": "Fill them with a free agent who plays today if a move above does not."})

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
        "moves": bd["moves"],
        "alternatives": bd["alternatives"],
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
    for m in bd["moves"]:
        new.append({"rec": "add", "player_id": m["add"]["id"], "name": m["add"]["name"], "team": m["add"]["team"],
                    "related_id": m["drop"]["id"], "related_name": m["drop"]["name"], "gain": m["gain"]})
    for m in bd["alternatives"]:
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
