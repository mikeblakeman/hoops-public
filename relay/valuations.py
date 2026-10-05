"""Player valuation tuned to this league's scoring settings.

Categories / roto: per-game z-scores per category. Ratio categories (FG%, FT%,
3PT%, A/TO) are volume-weighted: impact = makes - league_rate * attempts.
Reverse categories (TO) are negated.

Points: fantasy points per game using the league's own point values.

The "blend" line mixes ESPN's projection with actual season and last-15 stats.
Projection dominates early; actuals take over as games accrue.
"""
import math
from statistics import mean, pstdev

from .constants import RATIO_STATS, STATS_MAP

COUNT_STATS = [0, 6, 3, 2, 1, 17, 18, 11, 13, 14, 15, 16, 4, 5, 37, 38, 40]


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _p10(m, k=0.30, c=1.0):
    """P(a stat with per-game mean m reaches 10 in a game), normal approx."""
    sd = k * m + c
    return 1 - 0.5 * (1 + math.erf((9.5 - m) / (sd * math.sqrt(2))))


def dd_rate(pts, reb, ast):
    """Double-double rate from per-game PTS/REB/AST. Calibrated on 2025-26
    (441 players with 20+ games): RMSE about 0.03 per game."""
    a, b, c = _p10(pts), _p10(reb), _p10(ast)
    return a * b + a * c + b * c - 2 * a * b * c


def _line(row, key):
    """Per-game stat line for a split key (proj/season/l15/prev), or None."""
    gp = _num(row.get(f"{key}_gp")) or 0
    if gp <= 0:
        return None
    line = {"gp": gp}
    missing_dd = _num(row.get(f"{key}_DD")) is None
    for sid in COUNT_STATS:
        v = _num(row.get(f"{key}_{STATS_MAP[sid]}"))
        line[sid] = v if v is not None else 0.0
    if missing_dd:  # ESPN projections do not include double-doubles
        est = dd_rate(line[0], line[6], line[3])
        prev_dd, prev_gp = _num(row.get("prev_DD")), _num(row.get("prev_gp")) or 0
        line[37] = 0.5 * est + 0.5 * prev_dd if (prev_dd is not None and prev_gp >= 20) else est
    return line


def blend_line(row):
    proj, season = _line(row, "proj"), _line(row, "season")
    l15, prev = _line(row, "l15"), _line(row, "prev")
    # Last season only counts as a prior with a real sample
    prior = proj or (prev if prev and prev["gp"] >= 20 else None)
    gp = season["gp"] if season else 0
    parts = []
    if not season:
        if prior:
            parts = [(prior, 1.0)]
    else:
        w_prior = max(0.15, 1 - gp / 20) if prior else 0.0
        rest = 1 - w_prior
        if prior:
            parts.append((prior, w_prior))
        if l15 and l15["gp"] >= 3:
            parts += [(season, rest * 0.6), (l15, rest * 0.4)]
        else:
            parts.append((season, rest))
    if not parts:
        return None, {}
    out = {sid: sum(p[sid] * w for p, w in parts) for sid in COUNT_STATS}
    weights = {"w_prior": round(sum(w for p, w in parts if p is prior), 2) if prior else 0}
    return out, weights


def _cat_value(line, sid):
    """Raw per-game value for a category (ratio -> (num, den))."""
    if sid in RATIO_STATS:
        n, d = RATIO_STATS[sid]
        return (line.get(n, 0.0), line.get(d, 0.0))
    if sid == 22:  # adjusted FG% = (FGM + 0.5*3PM) / FGA
        return (line.get(13, 0.0) + 0.5 * line.get(17, 0.0), line.get(14, 0.0))
    return line.get(sid, 0.0)


def build(players_rows, settings):
    scoring_type = (settings.get("scoring_type") or "").upper()
    items = settings.get("scoring_items") or []
    team_count = settings.get("team_count") or 10
    slots = settings.get("lineup_slots") or {}
    roster_size = (settings.get("roster_size") or 13) - (slots.get("IR") or 0)
    pool_n = max(int(team_count * roster_size), 50)
    is_points = "POINT" in scoring_type
    warnings = []

    lines = []
    for r in players_rows:
        b, w = blend_line(r)
        if b is None:
            continue
        lines.append((r, b, w, {
            "proj": _line(r, "proj"), "l15": _line(r, "l15"),
            "prev": (lambda p: p if p and p["gp"] >= 20 else None)(_line(r, "prev"))}))

    if is_points:
        pts = {it["stat_id"]: (it.get("points") or 0) for it in items}
        unknown = [s for s in pts if s not in COUNT_STATS]
        if unknown:
            warnings.append(f"points stats not modeled: {[STATS_MAP.get(s, s) for s in unknown]}")

        def score(line):
            return sum(line.get(s, 0.0) * p for s, p in pts.items()) if line else None
        out = []
        for r, b, w, alt in lines:
            out.append(_row(r, b, w, score(b), {
                "value_proj": score(alt["proj"]), "value_l15": score(alt["l15"]),
                "value_prev": score(alt["prev"])}, {}))
        ranked, repl = _vorp(_rank(out), pool_n, team_count)
        return ranked, {"mode": "points", "pool_n": pool_n, "replacement_value": repl,
                        "warnings": warnings}

    cats = [(it["stat_id"], it.get("reverse", False)) for it in items]
    if not cats:
        warnings.append("no scoring items found; defaulting to 9-cat")
        cats = [(19, False), (20, False), (17, False), (6, False), (3, False),
                (2, False), (1, False), (11, True), (0, False)]
    for sid, _ in cats:
        if sid not in COUNT_STATS and sid not in RATIO_STATS and sid != 22:
            warnings.append(f"category not modeled: {STATS_MAP.get(sid, sid)}")

    def params(sample):
        p = {}
        for sid, _ in cats:
            vals = [_cat_value(b, sid) for b in sample]
            if vals and isinstance(vals[0], tuple):
                tot_n = sum(v[0] for v in vals)
                tot_d = sum(v[1] for v in vals)
                rate = tot_n / tot_d if tot_d else 0.0
                imp = [v[0] - rate * v[1] for v in vals]
                p[sid] = ("ratio", rate, mean(imp), pstdev(imp) or 1.0)
            else:
                p[sid] = ("count", None, mean(vals), pstdev(vals) or 1.0)
        return p

    def zs(line, p):
        if not line:
            return None
        z = {}
        for sid, rev in cats:
            kind, rate, mu, sd = p[sid]
            v = _cat_value(line, sid)
            x = (v[0] - rate * v[1]) if kind == "ratio" else v
            zz = (x - mu) / sd
            z[sid] = -zz if rev else zz
        return z

    # Two passes: rank everyone, then re-center on the top pool_n (rosterable players)
    p0 = params([b for _, b, _, _ in lines])
    first = sorted(lines, key=lambda t: -sum(zs(t[1], p0).values()))
    p1 = params([b for _, b, _, _ in first[:pool_n]])

    out = []
    for r, b, w, alt in lines:
        z = zs(b, p1)
        extra = {}
        for k in ("proj", "l15", "prev"):
            zz = zs(alt[k], p1)
            extra[f"value_{k}"] = sum(zz.values()) if zz else None
        out.append(_row(r, b, w, sum(z.values()), extra,
                        {f"z_{STATS_MAP.get(s, s)}": round(z[s], 2) for s, _ in cats}))
    ranked, repl = _vorp(_rank(out), pool_n, team_count)
    meta = {"mode": "categories", "pool_n": pool_n, "replacement_value": repl,
            "categories": [STATS_MAP.get(s, s) + (" (reverse)" if rv else "") for s, rv in cats],
            "league_rates": {STATS_MAP.get(s, s): round(v[1], 4) for s, v in p1.items() if v[0] == "ratio"},
            "params": {STATS_MAP.get(s, s): {"kind": v[0], "rate": v[1], "mu": v[2], "sd": v[3], "reverse": rv}
                       for (s, rv), v in ((c, p1[c[0]]) for c in cats)},
            "warnings": warnings}
    return ranked, meta


def _row(r, b, w, value, extra, zcols):
    fga, fta = b.get(14, 0.0), b.get(16, 0.0)
    row = {
        "player_id": r.get("player_id"), "name": r.get("name"), "pro_team": r.get("pro_team"),
        "eligible": r.get("eligible"), "status": r.get("status"), "on_team_id": r.get("on_team_id"),
        "injury_status": r.get("injury_status"), "pct_owned": r.get("pct_owned"),
        "pct_change": r.get("pct_change"), "adp": r.get("adp"), "espn_rank": r.get("espn_rank"),
        "season_gp": r.get("season_gp", 0) or 0, "proj_gp": r.get("proj_gp", "") or "",
        "w_prior": w.get("w_prior"),
        "min": round(b.get(40, 0.0), 1),
        "value": round(value, 2) if value is not None else None,
    }
    for k, v in extra.items():
        row[k] = round(v, 2) if v is not None else ""
    row["trend_l15"] = (round(extra["value_l15"] - value, 2)
                        if extra.get("value_l15") not in (None, "") and value is not None else "")
    row.update(zcols)
    for sid in (0, 6, 3, 2, 1, 17, 11):
        row[f"pg_{STATS_MAP[sid]}"] = round(b.get(sid, 0.0), 1)
    row["pg_FG%"] = round(b.get(13, 0.0) / fga, 3) if fga else ""
    row["pg_FGA"] = round(fga, 1)
    row["pg_FT%"] = round(b.get(15, 0.0) / fta, 3) if fta else ""
    row["pg_FTA"] = round(fta, 1)
    row["pg_FGM"] = round(b.get(13, 0.0), 2)
    row["pg_FTM"] = round(b.get(15, 0.0), 2)
    row["pg_3PA"] = round(b.get(18, 0.0), 2)
    row["pg_DD"] = round(b.get(37, 0.0), 3)
    return row


def _vorp(rows, pool_n, team_count):
    """Value over replacement. Replacement = the best players left on the wire
    (ranks just past the rosterable pool), since adds are freely available."""
    vals = [r["value"] for r in rows[pool_n:pool_n + max(team_count, 1)] if r["value"] is not None]
    repl = round(sum(vals) / len(vals), 2) if vals else 0.0
    for r in rows:
        if r["value"] is None:
            r["vorp_pg"] = r["vorp_season"] = ""
            continue
        r["vorp_pg"] = round(r["value"] - repl, 2)
        gp = _num(r.get("proj_gp")) or 60.0
        r["vorp_season"] = round(r["vorp_pg"] * min(gp, 82.0), 1)
    return rows, repl


def _rank(rows):
    rows.sort(key=lambda x: -(x["value"] if x["value"] is not None else -1e9))
    return [{"rank": i, **x} for i, x in enumerate(rows, 1)]
