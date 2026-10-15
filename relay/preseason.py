"""Preseason box scores from ESPN's public site API (cached, only new games fetched).

Writes:
  snapshots/preseason/boxscores.csv   one row per player per completed preseason game
  snapshots/preseason/summary.csv     per player: games, starts, minutes, per-36 lines
Preseason samples are tiny and minutes are compressed, so the board uses these
mainly as role signals (starts, minutes vs. expectation), not as projections.
"""
import csv
from datetime import date, timedelta
from pathlib import Path

SITE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
FIELDS = ["event_id", "date", "team", "player_id", "name", "starter", "dnp", "MIN", "FGM", "FGA", "3PM", "3PA",
          "FTM", "FTA", "OREB", "DREB", "REB", "AST", "STL", "BLK", "TO", "PTS"]
KEYMAP = {  # ESPN stat keys or names -> our fields
    "minutes": "MIN", "MIN": "MIN",
    "fieldGoalsMade-fieldGoalsAttempted": ("FGM", "FGA"), "FG": ("FGM", "FGA"),
    "threePointFieldGoalsMade-threePointFieldGoalsAttempted": ("3PM", "3PA"), "3PT": ("3PM", "3PA"),
    "freeThrowsMade-freeThrowsAttempted": ("FTM", "FTA"), "FT": ("FTM", "FTA"),
    "offensiveRebounds": "OREB", "OREB": "OREB", "defensiveRebounds": "DREB", "DREB": "DREB",
    "rebounds": "REB", "REB": "REB", "assists": "AST", "AST": "AST", "steals": "STL", "STL": "STL",
    "blocks": "BLK", "BLK": "BLK", "turnovers": "TO", "TO": "TO", "points": "PTS", "PTS": "PTS",
}


def _rows(p):
    p = Path(p)
    if not p.exists() or p.stat().st_size == 0:
        return []
    with p.open() as f:
        return list(csv.DictReader(f))


def _num(x):
    try:
        return float(str(x).split(":")[0])
    except (TypeError, ValueError):
        return 0.0


def parse_summary(js, event_id, day):
    out = []
    for team in ((js.get("boxscore") or {}).get("players") or []):
        abbr = (team.get("team") or {}).get("abbreviation", "")
        for block in team.get("statistics") or []:
            keys = block.get("keys") or block.get("names") or []
            for a in block.get("athletes") or []:
                ath = a.get("athlete") or {}
                row = {k: 0.0 for k in FIELDS[7:]}
                row.update({"event_id": event_id, "date": day, "team": abbr, "player_id": str(ath.get("id", "")),
                            "name": ath.get("displayName", ""), "starter": bool(a.get("starter")),
                            "dnp": bool(a.get("didNotPlay")) or not a.get("stats")})
                for k, v in zip(keys, a.get("stats") or []):
                    m = KEYMAP.get(k)
                    if isinstance(m, tuple):
                        parts = str(v).split("-")
                        if len(parts) == 2:
                            row[m[0]], row[m[1]] = _num(parts[0]), _num(parts[1])
                    elif m:
                        row[m] = _num(v)
                if row["MIN"] <= 0:
                    row["dnp"] = True
                out.append(row)
    return out


def update(get, out_dir, today, season_start=date(2026, 10, 20), first_day=date(2026, 9, 28)):
    """get(url, params) -> json. Returns (n_games_total, n_new, sample_block)."""
    out = Path(out_dir) / "preseason"
    out.mkdir(parents=True, exist_ok=True)
    box = _rows(out / "boxscores.csv")
    have = {r["event_id"] for r in box}
    new_rows, sample = [], None
    d = first_day
    last = min(today, season_start - timedelta(days=1))
    while d <= last:
        sb = get(f"{SITE}/scoreboard", {"dates": d.strftime("%Y%m%d"), "seasontype": 1})
        for ev in (sb or {}).get("events", []) or []:
            eid = str(ev.get("id"))
            done = (((ev.get("status") or {}).get("type") or {}).get("completed"))
            if not done or eid in have:
                continue
            js = get(f"{SITE}/summary", {"event": eid})
            rows = parse_summary(js or {}, eid, d.isoformat())
            if sample is None:
                pl = ((js or {}).get("boxscore") or {}).get("players") or []
                sample = {"keys": ((pl[0].get("statistics") or [{}])[0].get("keys") if pl else None),
                          "names": ((pl[0].get("statistics") or [{}])[0].get("names") if pl else None),
                          "first_athlete": (((pl[0].get("statistics") or [{}])[0].get("athletes") or [None])[0] if pl else None)}
            if rows:
                new_rows += rows
                have.add(eid)
        d += timedelta(days=1)
    box += [{k: r[k] for k in FIELDS} for r in new_rows]
    with (out / "boxscores.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(box)
    write_summary(box, out / "summary.csv")
    return len(have), len({r["event_id"] for r in new_rows}), sample


def write_summary(box, path):
    agg = {}
    for r in box:
        pid = r["player_id"]
        a = agg.setdefault(pid, {"player_id": pid, "name": r["name"], "team": r["team"], "games": 0, "starts": 0,
                                 "dnp": 0, **{k: 0.0 for k in FIELDS[7:]}})
        if str(r["dnp"]) in ("True", "true", "1") or _num(r["MIN"]) <= 0:
            a["dnp"] += 1
            continue
        a["games"] += 1
        a["starts"] += 1 if str(r["starter"]) in ("True", "true", "1") else 0
        a["team"] = r["team"]
        for k in FIELDS[7:]:
            a[k] += _num(r[k])
    rows = []
    for a in agg.values():
        g, mins = a["games"], a["MIN"]
        row = {"player_id": a["player_id"], "name": a["name"], "team": a["team"], "games": g, "starts": a["starts"],
               "dnp": a["dnp"], "mpg": round(mins / g, 1) if g else 0.0}
        for k in ("PTS", "REB", "AST", "STL", "BLK", "3PM", "3PA", "FGM", "FGA", "FTM", "FTA", "TO"):
            row[f"p36_{k}"] = round(a[k] * 36 / mins, 2) if mins else 0.0
        row["fg_pct"] = round(a["FGM"] / a["FGA"], 3) if a["FGA"] else ""
        row["ft_pct"] = round(a["FTM"] / a["FTA"], 3) if a["FTA"] else ""
        rows.append(row)
    rows.sort(key=lambda r: -r["mpg"])
    cols = list(rows[0].keys()) if rows else ["player_id"]
    with Path(path).open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
