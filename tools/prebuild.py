"""Build the finished brief docs inside the data job, so scheduled Claude runs only read JSON.

Why: scheduled (unattended) Claude runs are not allowed to execute code cloned from GitHub, so from Oct 24 to
Nov 6, 2026 almost every morning and pre-lock brief was assembled by hand from brief_data.json. The data job
already runs this code, so it now builds the briefs and publishes them in the mirror's prebuilt/ folder.

Usage: python tools/prebuild.py <public_snapshots_dir> <out_dir> [--now 2026-11-06T07:16]

Writes to <out_dir>:
  brief_morning.json  the morning brief doc. Built for TODAY before 7 PM Central and for TOMORROW from 7 PM on,
                      so the 7:23 AM task always finds a brief for its own day (late-evening data gets the usual
                      "Data warning" rebuild note; any later overnight run replaces it with fresher data).
  brief_prelock.json  the pre-lock doc for today.
  Each doc gains:
    prebuilt        {for_date, kind, built_ct, data_run_ct, error}
    lineup_options  lineup cards after each alternative add ("<player id>") or after skipping a planned add
                    ("hold:<player id>"), so a news-driven change needs no code to redraw the lineup card.
  call_values.csv     realized per-game value for every player over 1, 7 and 14 days from each recent start
                      date, plus call_values_meta.json (replacement value), for grading expert calls by lookup.
  status.json         what was built and any errors.

Built from the PUBLIC snapshot copy, so team names come out as "Team <id>" and "My team"; the scheduled task
restores them from the project doc claude/league/teams.json. Never raises: errors land in status.json.
"""
import argparse
import csv
import datetime as _dt
import json
import sys
import traceback
from pathlib import Path
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

EVENING_HOUR = 19        # from 7 PM Central the morning brief is built for the next day
MORNING_BUILD_TIME = (6, 30)
CALL_START_DAYS = 28     # call_values: start dates covered
HORIZONS = (1, 7, 14)


def _frozen(moment):
    class Frozen(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)
    return Frozen


def _set_clock(moment):
    """Point every module that reads the wall clock at `moment` (same approach as tests/__init__.py).
    Returns the previous clocks; pass them back to _restore_clock."""
    import brief
    from relay import lab, matchup, pending
    prev = {}
    for mod in (brief, lab, matchup, pending):
        prev[mod] = mod.datetime
        mod.datetime = _frozen(moment)
    return prev


def _restore_clock(prev):
    for mod, clock in prev.items():
        mod.datetime = clock


def lineup_options(snap, doc):
    import brief
    ids = doc.get("move_ids") or {}
    adds, drops, irs, bench = (list(ids.get(k) or []) for k in ("add", "drop", "ir", "bench"))
    # today's IR activations stay in every option: they come before any add (ESPN blocks adds until then)
    act, act_drop = (list(ids.get(k) or []) for k in ("activate", "activate_drop"))
    out = {}
    primary = [m for m in doc.get("moves") or [] if (m.get("add") or {}).get("id")]
    for alt in doc.get("alternatives") or []:
        a_id = str((alt.get("add") or {}).get("id") or "")
        a_drop = str((alt.get("drop") or {}).get("id") or "")
        base = next((m for m in primary if str((m.get("drop") or {}).get("id") or "") == a_drop
                     and str(m["add"]["id"]) in adds), None)
        if not a_id or not base or a_id in out:
            continue
        new_adds = [a_id if x == str(base["add"]["id"]) else x for x in adds]
        card = brief.lineup_after(snap, doc["date"], new_adds, drops, irs, bench, act, act_drop)
        if card:
            out[a_id] = dict(card, after_moves=[f"add {alt['add'].get('name')}" +
                                                (f" for {alt['drop'].get('name')}" if a_drop else "")])
    for m in primary:
        p_id, d_id = str(m["add"]["id"]), str((m.get("drop") or {}).get("id") or "")
        card = brief.lineup_after(snap, doc["date"], [x for x in adds if x != p_id],
                                  [x for x in drops if x != d_id or x in act_drop], irs, bench, act, act_drop)
        if card:
            out[f"hold:{p_id}"] = dict(card, after_moves=[f"skip adding {m['add'].get('name')}"])
    return out


def build_one(snap, kind, moment):
    import brief
    prev = _set_clock(moment)
    try:
        doc, _ = brief.build(snap, kind)
        try:
            doc["lineup_options"] = lineup_options(snap, doc)
        except Exception as e:  # options are a convenience; never lose the brief over them
            doc["lineup_options"] = {}
            doc.setdefault("notes", []).append(f"Lineup options could not be built: {e}")
        doc["prebuilt"] = {"for_date": doc.get("date"), "kind": kind,
                           "built_ct": _dt.datetime.now(CT).isoformat(timespec="minutes"),
                           "clock_ct": moment.isoformat(timespec="minutes"),
                           "data_run_ct": doc.get("data_run_ct"), "error": None}
        return doc, None
    finally:
        _restore_clock(prev)


def call_values(snap, out):
    import grade
    lines, last_day = grade.daily_lines(snap)
    meta = json.loads((Path(snap) / "valuation_meta.json").read_text())
    params, repl = meta.get("params", {}), meta.get("replacement_value", 0)
    names = {r["player_id"]: r.get("name", "") for r in grade.rows(Path(snap) / "valuations.csv")} \
        if (Path(snap) / "valuations.csv").exists() else {}
    if not last_day:
        return 0
    end_all = _dt.date.fromisoformat(last_day)
    starts = [end_all - _dt.timedelta(days=i) for i in range(1, CALL_START_DAYS + 1)]
    played = sorted({pid for day in lines.values() for pid in day})
    n = 0
    with (out / "call_values.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["player_id", "name", "start", "horizon_days", "games", "value_pg"])
        for pid in played:
            for s in starts:
                for h in HORIZONS:
                    e = s + _dt.timedelta(days=h)
                    if e > end_all:
                        continue  # horizon not finished yet
                    agg = {k: 0.0 for k in grade.FIELDS}
                    d = s
                    while d < e:
                        ln = lines.get(d.isoformat(), {}).get(pid)
                        if ln:
                            for k in grade.FIELDS:
                                agg[k] += ln[k]
                        d += _dt.timedelta(days=1)
                    v = grade.generic_value(agg, agg["GP"], params)
                    w.writerow([pid, names.get(pid, ""), s.isoformat(), h, int(agg["GP"]),
                                "" if v is None else round(v, 3)])
                    n += 1
    (out / "call_values_meta.json").write_text(json.dumps({
        "through": last_day, "replacement_value": repl,
        "rule": "A positive call (add, stream, start, buy, hold) hits when value_pg >= replacement_value; a negative "
                "call (sit, drop, sell) hits when value_pg < replacement_value. No games (blank value_pg): a "
                "positive call misses, a negative call hits. Look up the row with start = the call's date and "
                "horizon_days = the call's horizon (1, 7 or 14). Source hit rate shown to Mike = "
                "(hits + 2.5) / (calls + 5); 'small sample' under 50 calls."}, indent=1))
    return n


def main(snap, out, now=None):
    snap, out = Path(snap), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    now = now or _dt.datetime.now(CT)
    status = {"built_ct": now.isoformat(timespec="minutes"), "built": {}, "errors": []}
    if now.hour >= EVENING_HOUR:
        nxt = now.date() + _dt.timedelta(days=1)
        morning_at = _dt.datetime(nxt.year, nxt.month, nxt.day, *MORNING_BUILD_TIME, tzinfo=CT)
    else:
        morning_at = now
    for kind, moment in (("morning", morning_at), ("prelock", now)):
        try:
            doc, _ = build_one(str(snap), kind, moment)
            (out / f"brief_{kind}.json").write_text(json.dumps(doc, indent=1))
            status["built"][kind] = doc.get("date")
        except Exception:
            status["errors"].append(f"{kind}: {traceback.format_exc(limit=3)}")
    try:
        status["call_values_rows"] = call_values(snap, out)
    except Exception:
        status["errors"].append(f"call_values: {traceback.format_exc(limit=3)}")
    (out / "status.json").write_text(json.dumps(status, indent=1))
    return status


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("snap")
    ap.add_argument("out")
    ap.add_argument("--now", help="Central time to build as, e.g. 2026-11-06T07:16 (default: now)")
    a = ap.parse_args()
    t = _dt.datetime.fromisoformat(a.now).replace(tzinfo=CT) if a.now else None
    print(json.dumps(main(a.snap, a.out, t), indent=1))
