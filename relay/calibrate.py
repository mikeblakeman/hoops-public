"""Learn from results: how far do actual stat lines run from the pre-game projection?

Every player-game with a saved pre-game projection adds to a running ratio of
actual to projected for each stat. Ratios are shrunk toward 1.0 by sample size
and capped, and nothing is applied until there are enough player-games, so a few
hot or cold nights never move the model. The engine multiplies every player's
expected line by these factors (matchup.make_player).

Per-player learning already happens upstream: valuations.py blends the
projection with the player's actual season and last-15 lines. This module
corrects only the league-wide bias that blend cannot see.
"""
import json
from pathlib import Path

from . import history as H

KEYS = ["PTS", "REB", "AST", "STL", "BLK", "3PM", "3PA", "FGM", "FGA", "FTM", "FTA", "DD"]
MIN_GAMES = 600     # player-games before any factor is applied (about a week)
SHRINK_N = 1500     # factor moves halfway to the raw ratio at this many player-games
CAP = 0.08          # never move a stat by more than 8%


def compute(snap):
    snap = Path(snap)
    lines = H.daily_lines(snap)
    act = {k: 0.0 for k in KEYS}
    pro = {k: 0.0 for k in KEYS}
    n = 0
    days = []
    for day in sorted(lines):
        if not (snap / "history" / "proj" / f"{day}.csv").exists():
            continue                      # only true pre-game projections count
        proj, _ = H.proj_for(snap, day)
        used = 0
        for pid, ln in lines[day].items():
            p = proj.get(pid)
            if not p or ln["GP"] < 1:
                continue
            gp = ln["GP"]
            for k in KEYS:
                act[k] += ln[k]
                pro[k] += p[k] * gp
            n += gp
            used += 1
        if used:
            days.append(day)
    active = n >= MIN_GAMES
    w = n / (n + SHRINK_N) if n else 0.0
    raw, factors = {}, {}
    for k in KEYS:
        r = act[k] / pro[k] if pro[k] > 0 else 1.0
        raw[k] = round(r, 4)
        f = 1 + (r - 1) * w
        factors[k] = round(min(1 + CAP, max(1 - CAP, f)), 4) if active else 1.0
    return {"through": days[-1] if days else None, "days": len(days), "player_games": int(n),
            "min_games_needed": MIN_GAMES, "active": active, "raw_ratio": raw, "factors": factors}


def update(snap):
    out = compute(snap)
    (Path(snap) / "calibration.json").write_text(json.dumps(out, indent=1))
    return out


def load(snap):
    fp = Path(snap) / "calibration.json"
    try:
        d = json.loads(fp.read_text())
    except (OSError, ValueError):
        return {}
    return d.get("factors", {}) if d.get("active") else {}
