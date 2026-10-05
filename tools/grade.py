"""Grade logged decisions and expert calls against what actually happened.

Usage: python tools/grade.py snapshots [--since YYYY-MM-DD]

Daily stat lines come from consecutive season-total files in
snapshots/history/totals/ (file D = totals entering day D, so games on day D =
file(D+1) - file(D)).

Decisions: each "add" is compared with that day's "pass" alternatives, priced
with that day's matchup weights. Misses are alternatives that clearly beat
what was recommended; each is tagged with how highly the engine rated him, so
noise (rated low, one big game) is separated from model blind spots.

Expert calls: realized per-game value (league z-scores) over the call's
horizon, versus the waiver-wire replacement level. Sources get a hit rate
shrunk toward 50% until the sample is large.
"""
import argparse
import csv
import json
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "ledger"
COUNT = ["PTS", "REB", "AST", "STL", "BLK", "3PM", "FGM", "FTM", "DD", "TO"]
RATIO = {"FG%": ("FGM", "FGA"), "FT%": ("FTM", "FTA"), "3PT%": ("3PM", "3PA")}
FIELDS = ["PTS", "REB", "AST", "STL", "BLK", "3PM", "3PA", "FGM", "FGA", "FTM", "FTA", "DD", "TO", "MIN", "GP"]


def rows(p):
    p = Path(p)
    if not p.exists() or p.stat().st_size == 0:
        return []
    with p.open() as f:
        return list(csv.DictReader(f))


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def daily_lines(snap):
    files = sorted((Path(snap) / "history" / "totals").glob("*.csv"))
    tot = {fp.stem: {r["player_id"]: {k: f(r.get(k)) for k in FIELDS} for r in rows(fp)} for fp in files}
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
    return lines, (days[-1] if days else None)


def matchup_value(line, ctx):
    w = ctx["weights"]
    v = 0.0
    for c, wc in w.items():
        if c in RATIO:
            m, att = RATIO[c]
            team_pct = ctx["projected"].get(c, {}).get("mine") or 0
            team_att = (ctx.get("attempts") or {}).get(c) or 0
            if team_att:
                v += wc * (line.get(m, 0) - team_pct * line.get(att, 0)) / team_att
        else:
            v += wc * line.get(c, 0)
    return v


def generic_value(line, gp, params):
    if gp <= 0:
        return None
    v = 0.0
    for c, p in params.items():
        if p["kind"] == "ratio":
            m, att = RATIO[c]
            x = (line.get(m, 0) - p["rate"] * line.get(att, 0)) / gp
        else:
            x = line.get(c, 0) / gp
        z = (x - p["mu"]) / (p["sd"] or 1)
        v += -z if p.get("reverse") else z
    return v


def grade_decisions(lines, since):
    dec = [r for r in rows(LEDGER / "decisions.csv") if r["date"] >= since and r["date"] in lines]
    out, misses = [], []
    by_day = {}
    for r in dec:
        by_day.setdefault(r["date"], []).append(r)
    for day, rs in sorted(by_day.items()):
        cpath = LEDGER / "context" / f"{day}.json"
        if not cpath.exists():
            continue
        ctx = json.loads(cpath.read_text())
        L = lines[day]

        def val(pid):
            return matchup_value(L[pid], ctx) if pid in L else 0.0
        adds = [r for r in rs if r["rec"] == "add"]
        passes = [r for r in rs if r["rec"] == "pass"]
        alt_vals = sorted(val(r["player_id"]) for r in passes)
        median = alt_vals[len(alt_vals) // 2] if alt_vals else 0.0
        for r in adds:
            v = val(r["player_id"])
            out.append({"date": day, "rec": "add", "name": r["name"], "value": round(v, 4),
                        "alt_median": round(median, 4), "win": v >= median})
        best_add = max((val(r["player_id"]) for r in adds), default=0.0)
        for rank, r in enumerate(passes, 1):
            v = val(r["player_id"])
            if v > best_add + 0.05:
                misses.append({"date": day, "name": r["name"], "value": round(v, 4), "best_add": round(best_add, 4),
                               "engine_rank_among_alternatives": rank,
                               "kind": "near miss" if rank <= 2 else "likely noise or blind spot"})
    return out, misses


def grade_calls(lines, last_day, params, repl):
    calls = rows(LEDGER / "expert_calls.csv")
    graded = []
    for c in calls:
        if not c["player_id"]:
            continue
        h = int(f(c["horizon_days"]) or 7)
        start = date.fromisoformat(c["date"])
        end = start + timedelta(days=h)
        if not last_day or end.isoformat() > last_day:
            continue  # horizon not finished yet
        agg = {k: 0.0 for k in FIELDS}
        d = start
        while d < end:
            ln = lines.get(d.isoformat(), {}).get(c["player_id"])
            if ln:
                for k in FIELDS:
                    agg[k] += ln[k]
            d += timedelta(days=1)
        v = generic_value(agg, agg["GP"], params)
        positive = c["call"] in ("add", "stream", "start", "buy", "hold")
        if v is None:
            hit = not positive
        else:
            hit = (v >= repl) if positive else (v < repl)
        graded.append({"date": c["date"], "source": c["source"], "player": c["player"], "call": c["call"],
                       "games": agg["GP"], "value_pg": None if v is None else round(v, 2), "hit": hit})
    board = {}
    for g in graded:
        s = board.setdefault(g["source"], {"n": 0, "hits": 0})
        s["n"] += 1
        s["hits"] += int(g["hit"])
    for s in board.values():
        s["raw_rate"] = round(s["hits"] / s["n"], 3)
        s["shrunk_rate"] = round((s["hits"] + 2.5) / (s["n"] + 5), 3)
        s["trust"] = "enough data" if s["n"] >= 50 else "small sample"
    leaderboard = sorted(({"source": k, **v} for k, v in board.items()), key=lambda x: -x["shrunk_rate"])
    return graded, leaderboard


def calls_from_db(calls_dir, snap):
    """Convert exported page-database docs ({"date", "calls": [...]}) into ledger/expert_calls.csv."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import ledger as LG
    allc = []
    for fp in sorted(Path(calls_dir).rglob("*.json")):
        d = json.loads(fp.read_text())
        d = d.get("data", d)
        for c in d.get("calls", []):
            c.setdefault("date", d.get("date"))
            allc.append(c)
    tmp = Path(LEDGER) / "_calls_import.json"
    Path(LEDGER).mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(allc))
    LG.PATH = Path(LEDGER) / "expert_calls.csv"
    if LG.PATH.exists():
        LG.PATH.unlink()
    LG.add_calls(tmp, snap)
    tmp.unlink()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("snap")
    ap.add_argument("--since", default="2026-10-20")
    ap.add_argument("--ledger", help="ledger directory (default: repo ledger/)")
    ap.add_argument("--calls-dir", help="directory of expert-call JSON docs exported from the brief page database")
    a = ap.parse_args()
    global LEDGER
    if a.ledger:
        LEDGER = Path(a.ledger)
    if a.calls_dir:
        calls_from_db(a.calls_dir, a.snap)
    lines, last_day = daily_lines(a.snap)
    meta = json.loads((Path(a.snap) / "valuation_meta.json").read_text())
    dec, misses = grade_decisions(lines, a.since)
    calls, board = grade_calls(lines, last_day, meta.get("params", {}), meta.get("replacement_value", 0))
    LEDGER.mkdir(exist_ok=True)
    summary = {
        "through": last_day, "since": a.since,
        "adds_graded": len(dec), "adds_beat_alternatives": sum(1 for d in dec if d["win"]),
        "add_win_rate": round(sum(1 for d in dec if d["win"]) / len(dec), 3) if dec else None,
        "misses": sorted(misses, key=lambda m: -(m["value"] - m["best_add"]))[:5],
        "expert_calls_graded": len(calls),
        "sources": board,
    }
    (LEDGER / "summary.json").write_text(json.dumps(summary, indent=1))
    for name, data in (("graded_decisions.csv", dec), ("graded_calls.csv", calls)):
        if data:
            with (LEDGER / name).open("w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=list(data[0].keys()))
                w.writeheader()
                w.writerows(data)
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
