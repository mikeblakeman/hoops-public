"""Expert-call ledger.

Usage:
  python tools/ledger.py add-calls calls.json snapshots

calls.json is a list of {"date", "source", "url", "player", "call", "horizon_days", "note"}
where call is one of add, stream, start, sit, drop, buy, sell, hold.
Player names are resolved to ESPN ids from snapshots/valuations.csv. Only the
claim is stored (never article text). Duplicate (date, source, player, call)
rows are skipped.
"""
import csv
import json
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "ledger" / "expert_calls.csv"
FIELDS = ["date", "source", "url", "player_id", "player", "call", "horizon_days", "note"]
CALLS = {"add", "stream", "start", "sit", "drop", "buy", "sell", "hold"}


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    for t in (".", "'", "-", " jr", " sr", " iii", " ii"):
        s = s.replace(t, " ")
    return " ".join(s.split())


def load(path):
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open() as f:
        return list(csv.DictReader(f))


def add_calls(calls_file, snap):
    names = {}
    with open(Path(snap) / "valuations.csv") as f:
        for r in csv.DictReader(f):
            names[norm(r["name"])] = r["player_id"]
    rows = load(PATH)
    seen = {(r["date"], r["source"], r["player"].lower(), r["call"]) for r in rows}
    added, unresolved = 0, []
    for c in json.loads(Path(calls_file).read_text()):
        call = (c.get("call") or "").lower()
        if call not in CALLS:
            continue
        key = (c["date"], c["source"], c["player"].lower(), call)
        if key in seen:
            continue
        pid = names.get(norm(c["player"]), "")
        if not pid:
            unresolved.append(c["player"])
        rows.append({"date": c["date"], "source": c["source"], "url": c.get("url", ""), "player_id": pid,
                     "player": c["player"], "call": call, "horizon_days": c.get("horizon_days", 7),
                     "note": (c.get("note") or "")[:120]})
        seen.add(key)
        added += 1
    PATH.parent.mkdir(exist_ok=True)
    with PATH.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(json.dumps({"added": added, "total": len(rows), "unresolved": unresolved}))


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "add-calls":
        add_calls(sys.argv[2], sys.argv[3])
    else:
        print(__doc__)
        sys.exit(2)
