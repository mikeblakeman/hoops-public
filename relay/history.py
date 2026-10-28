"""Daily history helpers: per-day player stat lines (from season-total snapshots),
the pre-game projection saved each morning, and the lineups saved each run."""
import csv
from datetime import date, timedelta
from pathlib import Path

FIELDS = ["PTS", "REB", "AST", "STL", "BLK", "3PM", "3PA", "FGM", "FGA", "FTM", "FTA", "DD", "TO", "MIN", "GP"]
PROJ_KEYS = ["PTS", "REB", "AST", "STL", "BLK", "3PM", "3PA", "FGM", "FGA", "FTM", "FTA", "DD", "TO"]


def rows(p):
    p = Path(p)
    if not p.exists() or p.stat().st_size == 0:
        return []
    with p.open() as fh:
        return list(csv.DictReader(fh))


def num(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def daily_lines(snap):
    """{day: {player_id: stat line}} for each day that has a totals file the next day.
    File D holds season totals entering day D, so games on D = file(D+1) - file(D)."""
    files = sorted((Path(snap) / "history" / "totals").glob("*.csv"))
    tot = {fp.stem: {r["player_id"]: {k: num(r.get(k)) for k in FIELDS} for r in rows(fp)} for fp in files}
    days = sorted(tot)
    lines = {}
    for a, b in zip(days, days[1:]):
        if date.fromisoformat(b) - date.fromisoformat(a) != timedelta(days=1):
            continue
        day = {}
        for pid, t1 in tot[b].items():
            t0 = tot[a].get(pid, {k: 0.0 for k in FIELDS})
            d = {k: t1[k] - t0[k] for k in FIELDS}
            if d["GP"] >= 1:
                day[pid] = d
        lines[a] = day
    return lines


def _from_valuations(path):
    out = {}
    for r in rows(path):
        fga, fta = num(r.get("pg_FGA")), num(r.get("pg_FTA"))
        out[r["player_id"]] = {
            "PTS": num(r.get("pg_PTS")), "REB": num(r.get("pg_REB")), "AST": num(r.get("pg_AST")),
            "STL": num(r.get("pg_STL")), "BLK": num(r.get("pg_BLK")), "3PM": num(r.get("pg_3PM")),
            "3PA": num(r.get("pg_3PA")), "TO": num(r.get("pg_TO")), "FGA": fga, "FTA": fta,
            "FGM": num(r.get("pg_FGM")) or num(r.get("pg_FG%")) * fga,
            "FTM": num(r.get("pg_FTM")) or num(r.get("pg_FT%")) * fta,
            "DD": num(r.get("pg_DD")), "name": r.get("name", "")}
    return out


def save_proj(snap, day):
    """Keep the first pre-game projection of each day (the morning run). Later runs
    that day already include some of the day's games, so they never overwrite it."""
    snap = Path(snap)
    dest = snap / "history" / "proj" / f"{day}.csv"
    if dest.exists() or not (snap / "valuations.csv").exists():
        return False
    proj = _from_valuations(snap / "valuations.csv")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["player_id", "name"] + PROJ_KEYS)
        for pid, p in proj.items():
            w.writerow([pid, p["name"]] + [round(p[k], 3) for k in PROJ_KEYS])
    return True


def proj_for(snap, day):
    """(projection by player id, exact). exact=False when the day's own file was never
    saved and the current valuations stand in (slightly contaminated by later games)."""
    snap = Path(snap)
    fp = snap / "history" / "proj" / f"{day}.csv"
    if fp.exists():
        return {r["player_id"]: {**{k: num(r.get(k)) for k in PROJ_KEYS}, "name": r.get("name", "")}
                for r in rows(fp)}, True
    return _from_valuations(snap / "valuations.csv"), False


def save_scores(snap, day):
    """Matchup scoreboard as it stood before the day's games (first run before 5 PM CT).
    Day D's official results are the difference between scoreboards D+1 and D."""
    snap = Path(snap)
    src = snap / "current_scores.json"
    dest = snap / "history" / "scores" / f"{day}.json"
    if dest.exists() or not src.exists() or src.stat().st_size < 3:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(src.read_text())
    return True


def load_scores(snap, day):
    import json
    fp = Path(snap) / "history" / "scores" / f"{day}.json"
    try:
        return json.loads(fp.read_text())
    except (OSError, ValueError):
        return None


def save_final(snap, finals):
    """history/final/<matchup_period>.json: ESPN's final per-stat matchup totals for every team (from
    relay.normalize.final_scores). Rewritten while ESPN still changes them (stat corrections); returns the
    periods written."""
    import json
    out = []
    for mp, teams in sorted((finals or {}).items()):
        dest = Path(snap) / "history" / "final" / f"{mp}.json"
        txt = json.dumps({"matchup_period": mp, "teams": teams}, indent=1, sort_keys=True)
        if dest.exists() and dest.read_text() == txt:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(txt)
        out.append(mp)
    return out


def load_final(snap, mp):
    """{team_id: {stat_id: score}} for a finished matchup period, or None."""
    import json
    fp = Path(snap) / "history" / "final" / f"{mp}.json"
    try:
        return json.loads(fp.read_text()).get("teams")
    except (OSError, ValueError):
        return None
