"""Cached player bio (birth date, NBA experience) from ESPN's public core API.

Only players missing from the cache are fetched, so after the first run this
costs a handful of requests at most. Used for keeper (next-season) value.
"""
import csv
from pathlib import Path

CORE = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/athletes/{pid}"
FIELDS = ["player_id", "name", "dob", "age", "experience_years", "debut_year"]


def load(path):
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return {}
    with p.open() as f:
        return {r["player_id"]: r for r in csv.DictReader(f)}


def update(session, players, path, max_new=300, timeout=15):
    """players: iterable of (player_id, name). Returns (cache, fetched, failed)."""
    cache = load(path)
    todo = [(str(pid), name) for pid, name in players if str(pid) not in cache][:max_new]
    fetched = failed = 0
    for pid, name in todo:
        try:
            r = session.get(CORE.format(pid=pid), params={"lang": "en", "region": "us"},
                            timeout=timeout)
            if r.status_code != 200:
                failed += 1
                continue
            j = r.json()
            exp = j.get("experience") or {}
            cache[pid] = {
                "player_id": pid,
                "name": j.get("fullName") or name,
                "dob": (j.get("dateOfBirth") or "")[:10],
                "age": j.get("age", ""),
                "experience_years": exp.get("years", "") if isinstance(exp, dict) else "",
                "debut_year": j.get("debutYear", ""),
            }
            fetched += 1
        except Exception:
            failed += 1
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for row in sorted(cache.values(), key=lambda r: r["player_id"]):
            w.writerow(row)
    return cache, fetched, failed
