"""Strategy lab: test roster strategies on paper before switching (claude/engine/strategy-lab-spec.md).

One paper team per strategy (relay/strategies.py), `current` as the control. Every matchup week each paper team
resets to Mike's real roster from that morning's snapshot, plays the real opponent with real box scores, and makes
its own moves once a day from the data available before the day's first tip. A test is judged against paper
`current`, never against Mike's real results.

CLI
  python -m relay.lab daily snapshots [--ledger ledger/lab] [--now 2026-10-28T06:40]
      Idempotent. Scores every decided day that has the next day's totals, decides today (once, only before the
      first tip) for every strategy, rolls up finished weeks and writes ledger/lab/summary.json.
  python -m relay.lab replay snapshots --from 2026-10-20 --to 2026-10-27 [--ledger ledger/lab]
      For each day, materializes the last snapshot committed before that day's first tip
      (git archive <commit> snapshots), decides, then scores with the newest history. mode=backtest.
      Needs full git history: run it in an interactive session, not in the Action.

Files in ledger/lab/: state/<strategy>.json, decisions/<date>.json, days.csv, weeks.csv, summary.json.
"""
import argparse
import csv
import json
import math
import shutil
import subprocess
import sys
import tarfile
import tempfile
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import history as H
from . import matchup as MU
from . import strategies as S

CT = ZoneInfo("America/Chicago")
ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT / "ledger" / "lab"
COMP = ["PTS", "REB", "AST", "STL", "BLK", "3PM", "3PA", "FGM", "FGA", "FTM", "FTA", "DD"]
CATS = ["PTS", "FGM", "FTM", "3PM", "REB", "AST", "STL", "BLK", "DD", "FG%", "FT%", "3PT%"]
RATIO = MU.RATIO
SID = {"PTS": "0", "BLK": "1", "STL": "2", "AST": "3", "REB": "6", "FGM": "13", "FGA": "14", "FTM": "15",
       "FTA": "16", "3PM": "17", "3PA": "18", "DD": "37"}
PCT_SID = {"FG%": "19", "FT%": "20", "3PT%": "21"}
DAY_FIELDS = ["date", "matchup_period", "strategy", "mode", "starters", "games"] + COMP + ["adds"]
WEEK_FIELDS = ["matchup_period", "strategy", "mode", "start", "end", "days", "decided_days", "opponent_team_id"] + \
    COMP + ["FG%", "FT%", "3PT%", "cats_won", "smooth", "adds", "edge", "smooth_edge"]
RULE = ("A test is flagged when it has 4+ weeks of evidence (2+ live), wins 0.5+ more categories a week than the "
        "current plan, and is 90% likely to be better, not lucky.")
LIMITS = ("Limits: other managers do not react to paper moves, paper teams cannot react to late news the way the "
          "5:03 PM check can, and paper totals will differ from your real results. These apply to every paper team "
          "equally, so the comparison stays fair.")
MIN_WEEKS, MIN_LIVE, MAX_BACKTEST, MIN_EDGE, MAX_P, DISASTER = 4, 2, 2, 0.5, 0.10, 2.0
SWITCH_BACK_WEEKS = 3
FREE = MU.FREE


# ------------------------------------------------------------------ small helpers
def rows(p):
    return H.rows(p)


def write_csv(path, fields, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(data)
    if path.exists() and path.read_bytes() == tmp.read_bytes():
        tmp.unlink()
        return False
    tmp.replace(path)
    return True


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    txt = json.dumps(obj, indent=1, sort_keys=False)
    if path.exists() and path.read_text() == txt:
        return False
    path.write_text(txt)
    return True


def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def r3(x):
    return None if x is None else round(x, 3)


def first_tip(snap, day):
    """Earliest tip (HH:MM Central) on `day` from schedule.csv, or None when no NBA games."""
    tips = sorted(r["tip_ct"] for r in rows(Path(snap) / "schedule.csv") if r.get("date_ct") == day and r.get("tip_ct"))
    return tips[0] if tips else None


def strategy_cfg():
    """relay/strategy.json via relay.moves.load_strategy: active (what the brief follows) and control (what tests
    are measured against: the previous strategy after a switch, else the active one)."""
    from . import moves as MV
    return MV.load_strategy()


def active_strategy():
    return strategy_cfg()["active"]


def control_strategy():
    return strategy_cfg()["control"]


# ------------------------------------------------------------------ decision view (no hindsight)
def make_view(snap, day, dest=None):
    """A copy-free view of `snap` for a decision on `day`: every top-level file, but in history/ only the files
    dated BEFORE `day`. The decision code reads nothing else."""
    snap = Path(snap).resolve()
    dest = Path(dest or tempfile.mkdtemp(prefix=f"labview-{day}-"))
    dest.mkdir(parents=True, exist_ok=True)
    for child in snap.iterdir():
        if child.name == "history":
            continue
        (dest / child.name).symlink_to(child)
    hist = snap / "history"
    if hist.exists():
        for sub in hist.iterdir():
            if not sub.is_dir():
                continue
            (dest / "history" / sub.name).mkdir(parents=True, exist_ok=True)
            for fp in sub.iterdir():
                if fp.is_file() and fp.stem < day:
                    (dest / "history" / sub.name / fp.name).symlink_to(fp)
    return dest


# ------------------------------------------------------------------ paper rosters
real_rows = S.real_rows
protected_set = S.protected_set


def reset_state(sid, snap, D, mp, prow, day):
    rr = real_rows(D)
    prot = protected_set(snap, D, rr)
    st = {"strategy": sid, "matchup_period": mp, "week_start": prow["start"], "reset_on": day, "roster": rr,
          "protected": prot, "dropped": [], "violations": 0, "spot_b": None, "stacked": None,
          "status": "active", "days": []}
    if sid == "two_spots":
        # spot B: the lowest-value churnable hold, never the current streamer (spot A)
        from . import weekplan as WP
        players = [p for p in MU.team_players(D, D.my_tid) if p["slot"] != "IR"]
        occ_a = WP.occupant_of(D, players)
        cand = [p for p in players if str(p["id"]) not in prot and (not occ_a or p["id"] != occ_a["id"])]
        if cand:
            b = min(cand, key=lambda p: p["value"])
            st["spot_b"] = b["id"]
            for r in st["roster"]:
                if r["player_id"] == b["id"]:
                    r["acquired_ct"] = S.B_STAMP
    return st


def normalize_ir(st, D, day):
    """IR is legal only while ESPN shows the player OUT. Activate anyone who no longer is; if the active roster is
    full, drop the lowest-value churnable player without a game today (never a protected one)."""
    cap_active, _ = S.caps(D)
    notes = []
    for r in st["roster"]:
        if r.get("slot") != "IR":
            continue
        inj = ((D.vals.get(r["player_id"]) or {}).get("injury_status") or "").upper()
        if inj in MU.HARD_OUT:
            continue
        n_active = sum(1 for x in st["roster"] if x.get("slot") != "IR")
        if n_active >= cap_active:
            sp = next((s for s, d in D.sp_dates.items() if d == day), None)
            cands = []
            for x in st["roster"]:
                if x.get("slot") == "IR" or x["player_id"] in st["protected"]:
                    continue
                p = MU.make_player(D, x["player_id"])
                if p:
                    cands.append((sp is not None and MU.plays(D, p, sp), p["value"], x["player_id"], p["name"]))
            if not cands:
                notes.append(f"{r.get('name')} back from IR but no one can be dropped; left on IR")
                continue
            cands.sort()
            _, _, pid, name = cands[0]
            st["roster"] = [x for x in st["roster"] if x["player_id"] != pid]
            st["dropped"].append(pid)
            notes.append(f"forced: drop {name} to activate {r.get('name')}")
        r["slot"] = "BE"
        notes.append(f"activate {r.get('name')} from IR")
    return notes


def week_scores(lab, sid, mp, before):
    """Paper totals so far this matchup (scored days before `before`), as a component dict, or None."""
    tot, n = {c: 0.0 for c in COMP}, 0
    for r in rows(lab / "days.csv"):
        if r["strategy"] == sid and int(r["matchup_period"]) == mp and r["date"] < before:
            n += 1
            for c in COMP:
                tot[c] += float(r[c] or 0)
    return tot if n else None


def apply_moves(st, out, D, day):
    """Apply a decision to the paper roster under ESPN's rules. Returns (applied dict, notes)."""
    cap_active, cap_ir = S.caps(D)
    by = {r["player_id"]: r for r in st["roster"]}
    applied, notes = {"ir": [], "adds": [], "drops": []}, []
    for pid in out.get("ir", []):
        r = by.get(pid)
        inj = ((D.vals.get(pid) or {}).get("injury_status") or "").upper()
        n_ir = sum(1 for x in st["roster"] if x.get("slot") == "IR")
        if not r or r.get("slot") == "IR":
            continue
        if inj not in MU.HARD_OUT:
            notes.append(f"skipped IR for {r.get('name')}: ESPN does not show him out")
            continue
        if n_ir >= cap_ir:
            notes.append(f"skipped IR for {r.get('name')}: IR is full")
            continue
        r["slot"] = "IR"
        applied["ir"].append(pid)
    for pid in out.get("drops", []):
        if pid in st["protected"]:
            st["violations"] += 1          # never reached: strategies.decide filters these first
            notes.append(f"refused drop of protected player {pid}")
            continue
        if pid in by:
            st["roster"] = [x for x in st["roster"] if x["player_id"] != pid]
            by.pop(pid)
            if pid not in st["dropped"]:
                st["dropped"].append(pid)
            applied["drops"].append(pid)
    for i, pid in enumerate(out.get("adds", [])):
        if pid in by:
            continue
        v = D.vals.get(pid) or {}
        if v.get("status") not in FREE:
            notes.append(f"skipped add of {v.get('name', pid)}: not available")
            continue
        if sum(1 for x in st["roster"] if x.get("slot") != "IR") >= cap_active:
            notes.append(f"skipped add of {v.get('name', pid)}: no roster room")
            continue
        row = {"player_id": pid, "name": v.get("name", ""), "slot": "BE", "acquisition_type": "ADD",
               "acquired_ct": S.B_STAMP if pid == out.get("spot_b") else f"{day}T07:{i:02d}"}
        st["roster"].append(row)
        by[pid] = row
        st["dropped"] = [x for x in st["dropped"] if x != pid]
        applied["adds"].append(pid)
    if "spot_b" in out:
        st["spot_b"] = out["spot_b"] if (out["spot_b"] in by) else st.get("spot_b") if st.get("spot_b") in by else None
    if "stacked" in out:
        st["stacked"] = out["stacked"]
    return applied, notes


def paper_lineup(snap, day, st, scores, bench=()):
    """Today's lineup for the paper roster: the engine's assign() with the matchup key, after today's moves."""
    D = MU.Data(snap, my_roster=st["roster"], my_scores=scores, free_ids=st["dropped"])
    c = MU._context(D, date.fromisoformat(day))
    if not c or not c["sps"] or D.sp_dates.get(c["sps"][0]) != day:
        return []
    sp0, bench = c["sps"][0], set(bench)
    playing = [p for p in c["mine"] if p["slot"] != "IR" and p["avail"] > 0 and MU.plays(D, p, sp0)
               and p["id"] not in bench]
    st_, _, _ = MU.assign(playing, D.slots, c["key"], True)
    return [{"id": p["id"], "name": p["name"], "slot": slot} for p, slot in st_]


def fallback_lineup(snap, day, st):
    """A day no data run landed before first tip: no moves, and a lineup that uses no news from that day
    (every active player with a game, best season value first; anyone who sits scores nothing)."""
    D = MU.Data(snap, my_roster=st["roster"], free_ids=st["dropped"])
    sp = next((s for s, d in D.sp_dates.items() if d == day), None)
    if sp is None:
        return []
    playing = [p for p in MU.team_players(D, D.my_tid) if p["slot"] != "IR" and MU.plays(D, p, sp)]
    st_, _, _ = MU.assign(playing, D.slots, lambda p: p["value"])
    return [{"id": p["id"], "name": p["name"], "slot": slot} for p, slot in st_]


def name_of(D, pid):
    return (D.vals.get(pid) or {}).get("name") or pid


def today_text(D, applied, notes_out):
    """Today's paper moves in plain words for the page (only moves that were actually applied)."""
    if not (applied["ir"] or applied["adds"] or applied["drops"]):
        return []
    txt = []
    for n in notes_out:
        if n.endswith(" to IR"):
            txt.append("Move " + n)
        elif n.startswith(("add ", "bench ")):
            txt.append(n[0].upper() + n[1:])
        elif n.startswith("spot B: add "):
            txt.append("Spot B: add " + n[len("spot B: add "):])
    return txt or ([f"Move {name_of(D, p)} to IR" for p in applied["ir"]] +
                   [f"Add {name_of(D, p)}" for p in applied["adds"]] + [f"Drop {name_of(D, p)}" for p in applied["drops"]])


# ------------------------------------------------------------------ deciding
def decide_day(snap, day, lab=None, mode="live", run=None, missed=False):
    """Decide `day` for every strategy from snapshot directory `snap` (written before the day's first tip)."""
    lab = Path(lab or LAB)
    view = make_view(snap, day) if not missed else Path(snap)
    try:
        D0 = MU.Data(view)
        mp, sps, prow = MU.current_period(D0, date.fromisoformat(day))
        if mp is None or not sps or D0.sp_dates.get(sps[0]) != day:
            return None
        rec = {"date": day, "matchup_period": mp, "mode": "missed" if missed else mode,
               "data_run_ct": D0.status.get("run_ct"), "first_tip_ct": first_tip(view, day), "run": run or {},
               "strategies": {}}
        for sid in S.ORDER:
            sp_ = lab / "state" / f"{sid}.json"
            st = load_json(sp_)
            if st and st.get("status") == "retired":
                rec["strategies"][sid] = {"retired": True}
                continue
            if not st or st.get("matchup_period") != mp:
                st = reset_state(sid, view, D0, mp, prow, day)
            entry = {"notes": normalize_ir(st, D0, day)}
            scores = week_scores(lab, sid, mp, day)
            if missed:
                out = S.result(notes=["no data run before first tip: no moves"])
            else:
                ctx = S.Ctx(view, date.fromisoformat(day), st["roster"], st["dropped"], scores, st)
                try:
                    out = S.decide(sid, ctx, st["roster"], set(st["protected"]), date.fromisoformat(day))
                except Exception as e:  # noqa: BLE001  one strategy failing never stops the others
                    out = S.result(notes=[f"error: {type(e).__name__}: {e}"])
                    entry["error"] = traceback.format_exc(limit=4)
            Dp = MU.Data(view, my_roster=st["roster"], my_scores=scores, free_ids=st["dropped"])
            applied, notes = apply_moves(st, out, Dp, day)
            if missed:
                lineup = fallback_lineup(view, day, st)
            else:
                lineup = paper_lineup(view, day, st, scores, out.get("bench", []))
            st["days"] = sorted(set(st.get("days", [])) | {day})
            st["last_date"] = day
            entry.update(decision={k: out.get(k, []) for k in ("ir", "adds", "drops", "bench")},
                         applied=applied, today=today_text(Dp, applied, out.get("notes", [])),
                         notes=entry["notes"] + out.get("notes", []) + notes,
                         lineup=lineup, roster=[r["player_id"] for r in st["roster"]],
                         ir=[r["player_id"] for r in st["roster"] if r.get("slot") == "IR"],
                         protected=st["protected"])
            rec["strategies"][sid] = entry
            write_json(sp_, st)
        write_json(lab / "decisions" / f"{day}.json", rec)
        return rec
    finally:
        if not missed:
            shutil.rmtree(view, ignore_errors=True)


# ------------------------------------------------------------------ scoring
def sum_lines(lines, ids):
    tot, n = {c: 0.0 for c in COMP}, 0
    for pid in ids:
        x = lines.get(str(pid))
        if x:
            n += 1
            for c in COMP:
                tot[c] += x.get(c, 0.0)
    return tot, n


def opponent_of(snap, mp, tid):
    for m in rows(Path(snap) / "matchups.csv"):
        if int(MU._num(m["matchup_period"])) != int(mp):
            continue
        h, a = str(m["home_team_id"]), str(m["away_team_id"])
        if h == str(tid):
            return a
        if a == str(tid):
            return h
    return None


def saved_lineup(snap, day, tid):
    """Starters of a real team on `day` from history/lineups (every slot but bench and IR)."""
    return [r["player_id"] for r in rows(Path(snap) / "history" / "lineups" / f"{day}.csv")
            if str(r["team_id"]) == str(tid) and r.get("slot") not in ("BE", "IR", "")]


def score_days(hist, lab=None):
    """Score every decided day whose stat lines exist (needs the next day's totals). Returns days scored."""
    hist, lab = Path(hist), Path(lab or LAB)
    lines = H.daily_lines(hist)
    status = load_json(hist / "status.json", {}) or {}
    me = str(status.get("my_team_id"))
    have = rows(lab / "days.csv")
    done = {(r["date"], r["strategy"]) for r in have}
    new = []
    for fp in sorted((lab / "decisions").glob("*.json")) if (lab / "decisions").exists() else []:
        day = fp.stem
        if day not in lines:
            continue
        rec = load_json(fp, {})
        mp = rec.get("matchup_period")
        for sid, e in (rec.get("strategies") or {}).items():
            if e.get("retired") or (day, sid) in done:
                continue
            tot, n = sum_lines(lines[day], [p["id"] for p in e.get("lineup", [])])
            new.append(dict(tot, date=day, matchup_period=mp, strategy=sid, mode=rec.get("mode"),
                            starters=len(e.get("lineup", [])), games=n, adds=len(e["applied"]["adds"])))
        opp = opponent_of(hist, mp, me)
        for who, tid in (("real", me), ("opponent", opp)):
            if (day, who) in done or tid is None:
                continue
            ids = saved_lineup(hist, day, tid)
            tot, n = sum_lines(lines[day], ids)
            new.append(dict(tot, date=day, matchup_period=mp, strategy=who, mode="actual", starters=len(ids),
                            games=n, adds=""))
    if new:
        allr = have + [{k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items()} for r in new]
        allr.sort(key=lambda r: (r["date"], (S.ORDER + ["real", "opponent"]).index(r["strategy"])
                                 if r["strategy"] in S.ORDER + ["real", "opponent"] else 99))
        write_csv(lab / "days.csv", DAY_FIELDS, allr)
    return len({r["date"] for r in new})


# ------------------------------------------------------------------ weeks
def cat_values(t):
    out = {c: t.get(c, 0.0) for c in CATS if c not in RATIO}
    for c, (m, a) in RATIO.items():
        out[c] = (t[m] / t[a]) if t.get(a) else None
    return out


def from_final(sbs):
    t = {c: MU._num((sbs or {}).get(sid)) for c, sid in SID.items()}
    for c, (m, a) in RATIO.items():   # ESPN stores attempts too; fall back to the percentage if not
        if not t.get(a) and MU._num((sbs or {}).get(PCT_SID[c])):
            t[a] = t[m] / MU._num(sbs[PCT_SID[c]])
    return t


def team_week(hist, mp, tid, dates, lines, use_final=True):
    """A real team's totals for a matchup: ESPN's final if saved, else its saved lineups times daily lines."""
    fin = H.load_final(hist, mp) if use_final else None
    if fin and str(tid) in fin:
        return from_final(fin[str(tid)]), "final"
    tot = {c: 0.0 for c in COMP}
    for d in dates:
        if d not in lines:
            continue
        t, _ = sum_lines(lines[d], saved_lineup(hist, d, tid))
        for c in COMP:
            tot[c] += t[c]
    return tot, "lineups"


def compare(mine, theirs):
    a, b = cat_values(mine), cat_values(theirs)
    won = 0.0
    for c in CATS:
        x, y = a[c], b[c]
        if x is None or y is None or abs(x - y) < 1e-9:
            won += 0.5 if (x is None) == (y is None) else (1.0 if y is None else 0.0)
        elif x > y:
            won += 1.0
    return won


def smooth(mine, theirs, scale):
    a, b = cat_values(mine), cat_values(theirs)
    s = 0.0
    for c in CATS:
        x, y = a[c], b[c]
        if x is None or y is None:
            s += 0.5
            continue
        sc = scale.get(c) or 0.0
        s += MU.Phi((x - y) / sc) if sc > 1e-12 else (1.0 if x > y else 0.5 if x == y else 0.0)
    return s


def league_scale(hist, weeks, lines):
    """Per-category standard deviation of weekly team totals, pooled over completed weeks (each week around its
    own mean, so 6- and 7-day weeks pool fairly)."""
    teams = sorted({str(r["team_id"]) for r in rows(Path(hist) / "teams.csv")})
    ss, df = {c: 0.0 for c in CATS}, {c: 0 for c in CATS}
    for mp, dates in weeks:
        vals = {c: [] for c in CATS}
        for t in teams:
            cv = cat_values(team_week(hist, mp, t, dates, lines)[0])
            for c in CATS:
                if cv[c] is not None:
                    vals[c].append(cv[c])
        for c in CATS:
            v = vals[c]
            if len(v) >= 2:
                m = sum(v) / len(v)
                ss[c] += sum((x - m) ** 2 for x in v)
                df[c] += len(v) - 1
    return {c: math.sqrt(ss[c] / df[c]) if df[c] else None for c in CATS}


def period_dates(hist):
    out = {}
    for r in rows(Path(hist) / "matchup_calendar.csv"):
        s, e = date.fromisoformat(r["start"]), date.fromisoformat(r["end"])
        out[int(r["matchup_period"])] = [(s + timedelta(days=i)).isoformat() for i in range((e - s).days + 1)]
    return out


def rollup(hist, lab=None):
    """weeks.csv: one row per finished matchup and strategy (plus Mike's real result and the opponent)."""
    hist, lab = Path(hist), Path(lab or LAB)
    lines = H.daily_lines(hist)
    days = rows(lab / "days.csv")
    if not days:
        return []
    pdates = period_dates(hist)
    me = str((load_json(hist / "status.json", {}) or {}).get("my_team_id"))
    decided = {fp.stem: load_json(fp, {}) for fp in (lab / "decisions").glob("*.json")} \
        if (lab / "decisions").exists() else {}
    last_scored = max(r["date"] for r in days if r["strategy"] == "opponent") if any(
        r["strategy"] == "opponent" for r in days) else ""
    finished = [(mp, ds) for mp, ds in sorted(pdates.items())
                if ds and ds[-1] <= last_scored and any(int(r["matchup_period"]) == mp for r in days)]
    scale = league_scale(hist, finished, lines)
    control = control_strategy()
    out = []
    for mp, ds in finished:
        opp = opponent_of(hist, mp, me)
        opp_t, _src = team_week(hist, mp, opp, ds, lines)
        wk = {}
        for sid in S.ORDER + ["real"]:
            rs = [r for r in days if r["strategy"] == sid and int(r["matchup_period"]) == mp]
            if not rs:
                continue
            if sid == "real":
                tot, mode = team_week(hist, mp, me, ds, lines)[0], "actual"
            else:
                tot = {c: sum(float(r[c] or 0) for r in rs) for c in COMP}
                modes = {(decided.get(d) or {}).get("mode") for d in ds if
                         ((decided.get(d) or {}).get("strategies") or {}).get(sid) is not None}
                n_dec = sum(1 for d in ds if ((decided.get(d) or {}).get("strategies") or {}).get(sid))
                games_days = sum(1 for d in ds if lines.get(d))
                mode = "live" if modes == {"live"} and n_dec >= games_days else "backtest"
            cv = cat_values(tot)
            wk[sid] = dict({c: round(tot[c], 3) for c in COMP}, **{c: r3(cv[c]) for c in RATIO},
                           matchup_period=mp, strategy=sid, mode=mode, start=ds[0], end=ds[-1], days=len(ds),
                           decided_days=len(rs), opponent_team_id=opp, cats_won=compare(tot, opp_t),
                           smooth=round(smooth(tot, opp_t, scale), 3),
                           adds=sum(int(r["adds"] or 0) for r in rs) if sid != "real" else "")
        ctl = wk.get(control)
        for sid, w in wk.items():
            if ctl is not None:
                w["edge"] = round(w["cats_won"] - ctl["cats_won"], 3)
                w["smooth_edge"] = round(w["smooth"] - ctl["smooth"], 3)
            out.append(w)
        cvo = cat_values(opp_t)
        real_t = team_week(hist, mp, me, ds, lines)[0]
        out.append(dict({c: round(opp_t[c], 3) for c in COMP}, **{c: r3(cvo[c]) for c in RATIO},
                        matchup_period=mp, strategy="opponent", mode="actual", start=ds[0], end=ds[-1],
                        days=len(ds), decided_days="", opponent_team_id=me, cats_won=compare(opp_t, real_t),
                        smooth="", adds=""))
    write_csv(lab / "weeks.csv", WEEK_FIELDS, out)
    return out


# ------------------------------------------------------------------ proof rule (spec section 5)
def _betacf(a, b, x):
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > 1e-300 else 1e-300)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > 1e-300 else 1e-300)
        c = 1.0 + aa / c if abs(c) > 1e-300 else 1e-300
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > 1e-300 else 1e-300)
        c = 1.0 + aa / c if abs(c) > 1e-300 else 1e-300
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 3e-12:
            break
    return h


def betainc(a, b, x):
    """Regularized incomplete beta I_x(a, b)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbt = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x)
    if x < (a + 1) / (a + b + 2):
        return math.exp(lbt) * _betacf(a, b, x) / a
    return 1.0 - math.exp(lbt) * _betacf(b, a, 1 - x) / b


def t_cdf(t, df):
    """Student t cumulative distribution."""
    x = df / (df + t * t)
    tail = 0.5 * betainc(df / 2.0, 0.5, x)
    return 1.0 - tail if t > 0 else tail


def paired(diffs):
    """(mean, one-sided p that the true mean is <= 0, one-sided p that it is >= 0); p is None under 2 weeks."""
    n = len(diffs)
    if not n:
        return None, None, None
    m = sum(diffs) / n
    if n < 2:
        return m, None, None
    sd = math.sqrt(sum((d - m) ** 2 for d in diffs) / (n - 1))
    if sd < 1e-12:
        return m, (0.0 if m > 0 else 1.0), (0.0 if m < 0 else 1.0)
    t = m / (sd / math.sqrt(n))
    return m, 1.0 - t_cdf(t, n - 1), t_cdf(t, n - 1)


def evaluate(weeks, violations=0):
    """Season numbers and status for one test strategy from its paired weeks:
    [{"matchup_period", "mode", "cats_won", "edge", "smooth_edge"}] (finished weeks, edge vs control)."""
    live = [w for w in weeks if w["mode"] == "live"]
    back = sorted([w for w in weeks if w["mode"] != "live"], key=lambda w: w["matchup_period"])[-MAX_BACKTEST:]
    used = sorted(live + back, key=lambda w: w["matchup_period"])
    n = len(used)
    if not n:
        return {"weeks": 0, "live_weeks": 0, "avg_cats_won": None, "edge": None, "confidence": None}, "testing"
    edge = sum(w["edge"] for w in used) / n
    ms, p_up, p_down = paired([w["smooth_edge"] for w in used])
    season = {"weeks": n, "live_weeks": len(live), "avg_cats_won": round(sum(w["cats_won"] for w in used) / n, 2),
              "edge": round(edge, 2), "confidence": None if p_up is None else round(1 - p_up, 3),
              "smooth_edge": round(ms, 3), "worst_week": round(min(w["edge"] for w in used), 2)}
    enough = n >= MIN_WEEKS and len(live) >= MIN_LIVE
    if (enough and edge >= MIN_EDGE and p_up is not None and p_up <= MAX_P and ms > 0
            and min(w["edge"] for w in used) >= -DISASTER and violations == 0):
        return season, "ready"
    if n >= MIN_WEEKS and edge <= -MIN_EDGE and p_down is not None and p_down <= MAX_P:
        return season, "retired"
    if edge > 0:
        return season, "ahead"
    if edge < 0:
        return season, "behind"
    return season, "testing"


# ------------------------------------------------------------------ summary (the brief's `lab` block)
def summary(hist, lab=None, now=None):
    hist, lab = Path(hist), Path(lab or LAB)
    now = now or datetime.now(CT)
    weeks = rows(lab / "weeks.csv")
    days = rows(lab / "days.csv")
    cfg = strategy_cfg()
    control, active = cfg["control"], cfg["active"]
    decs = sorted((lab / "decisions").glob("*.json")) if (lab / "decisions").exists() else []
    last = load_json(decs[-1], {}) if decs else {}
    me = str((load_json(hist / "status.json", {}) or {}).get("my_team_id"))
    mp = last.get("matchup_period")
    scored = sorted({r["date"] for r in days if r["strategy"] == "opponent" and mp is not None
                     and int(r["matchup_period"]) == mp})
    opp_tot = {c: sum(float(r[c] or 0) for r in days if r["strategy"] == "opponent" and r["date"] in scored)
               for c in COMP}
    opp = opponent_of(hist, mp, me) if mp else None
    out_s, best, switch_back = [], None, None
    for sid in S.ORDER:
        name, summ, effort = S.INFO[sid]
        st = load_json(lab / "state" / f"{sid}.json", {}) or {}
        mine = [w for w in weeks if w["strategy"] == sid]
        ctl = {w["matchup_period"]: w for w in weeks if w["strategy"] == control}
        paired_w = [{"matchup_period": int(w["matchup_period"]), "mode": w["mode"], "cats_won": float(w["cats_won"]),
                     "edge": float(w["edge"] or 0), "smooth_edge": float(w["smooth_edge"] or 0), "start": w["start"]}
                    for w in mine if w["matchup_period"] in ctl]
        season, status = evaluate(paired_w, int(st.get("violations") or 0))
        if sid == control:
            status = "control"
            season.update(edge=0.0, confidence=None)
        elif sid in S.REFERENCE:
            status = "reference"
        elif sid == active:
            # in use after a switch: never flagged or retired; "Switch back?" when it trails the previous strategy
            # by 0.5+ categories a week over 3+ live weeks since the switch with 90% confidence (spec section 6)
            status = "ahead" if (season.get("edge") or 0) > 0 else "behind" if (season.get("edge") or 0) < 0 \
                else "testing"
            since = [w for w in paired_w if w["mode"] == "live" and (not cfg["since"] or w["start"] >= cfg["since"])]
            m, _p_up, p_down = paired([w["smooth_edge"] for w in since])
            edge_live = sum(w["edge"] for w in since) / len(since) if since else 0.0
            if len(since) >= SWITCH_BACK_WEEKS and edge_live <= -MIN_EDGE and p_down is not None and p_down <= MAX_P:
                switch_back = {"id": control, "from": sid, "edge": round(edge_live, 2), "weeks": len(since),
                               "confidence": round(1 - p_down, 3),
                               "summary": f"{S.INFO[sid][0]} trails {S.INFO[control][0]} by {-edge_live:.1f} "
                                          f"categories a week over {len(since)} live weeks, "
                                          f"{round((1 - p_down) * 100)}% sure."}
        elif st.get("status") == "retired":
            status = "retired"
        elif status == "retired" and st:
            st["status"] = "retired"          # it stops running from the next decision
            write_json(lab / "state" / f"{sid}.json", st)
        wk_rows = [r for r in days if r["strategy"] == sid and r["date"] in scored]
        week = {"cats_won": None, "days": len(wk_rows)}
        if wk_rows:
            tot = {c: sum(float(r[c] or 0) for r in wk_rows) for c in COMP}
            week["cats_won"] = compare(tot, opp_tot)
        adds = [int(r["adds"]) for r in days if r["strategy"] == sid and r["adds"] not in ("", None)]
        e = (last.get("strategies") or {}).get(sid) or {}
        today = e.get("today", []) if last.get("date") == now.date().isoformat() else []
        out_s.append({"id": sid, "name": name, "summary": summ, "effort": effort, "is_control": sid == control,
                      "week": week, "season": season,
                      "adds_per_day": round(sum(adds) / len(adds), 2) if adds else None,
                      "status": status, "today": today})
        if status == "ready" and sid != active and (best is None or season["edge"] > best[1]["edge"]):
            best = (sid, season)
    ready = None
    if best:
        sid, se = best
        ready = {"id": sid, "name": S.INFO[sid][0], "edge": se["edge"], "weeks": se["weeks"],
                 "live_weeks": se["live_weeks"], "confidence": se["confidence"],
                 "summary": f"{S.INFO[sid][0]} wins {se['edge']:+.1f} categories a week over "
                            f"{'the current plan' if control == active else S.INFO[control][0]} across "
                            f"{se['weeks']} weeks ({se['live_weeks']} live), {round(se['confidence'] * 100)}% sure."}
    back = sorted({int(w["matchup_period"]) for w in weeks if w["strategy"] in S.ORDER and w["mode"] != "live"})
    notes = []
    if back:
        lbl = " and ".join(str(b) for b in back) if len(back) <= 2 else ", ".join(str(b) for b in back)
        notes.append(f"Week{'s' if len(back) > 1 else ''} {lbl} {'were' if len(back) > 1 else 'was'} replayed from "
                     "saved data (backtest). At most 2 backtest weeks count as evidence.")
    modes_now = {(load_json(fp, {}) or {}).get("mode") for fp in decs if mp is not None
                 and (load_json(fp, {}) or {}).get("matchup_period") == mp}
    if mp is not None and mp not in back and modes_now - {"live"}:
        notes.append(f"Week {mp} is partly replayed or had a missed morning, so it will count as backtest.")
    if last.get("mode") == "missed" and last.get("date") == now.date().isoformat():
        notes.append("No data run landed before today's first tip, so the paper teams made no moves today.")
    notes.append(LIMITS)
    doc = {"control": control, "active": active, "since": cfg["since"], "rule": RULE,
           "week": {"matchup_period": mp, "days_scored": len(scored), "opponent_team_id": opp,
                    "opponent": f"Team {opp}" if opp else None},
           "decided": last.get("date"), "decision_mode": last.get("mode"),
           "strategies": out_s, "ready": ready, "switch_back": switch_back, "notes": notes}
    fp = lab / "summary.json"
    old = load_json(fp, {}) or {}
    if {k: v for k, v in old.items() if k != "updated_ct"} == doc:
        return old
    doc = dict({"updated_ct": now.isoformat(timespec="minutes")}, **doc)
    write_json(fp, doc)
    return doc


# ------------------------------------------------------------------ CLI
def daily(snap, lab=None, now=None):
    """Idempotent live step: score, decide today if before first tip, roll up, summarize."""
    snap, lab = Path(snap), Path(lab or LAB)
    now = now or datetime.now(CT)
    day = now.date().isoformat()
    score_days(snap, lab)
    tip = first_tip(snap, day)
    dec = lab / "decisions" / f"{day}.json"
    res = {"date": day, "first_tip_ct": tip, "decided": False}
    status = load_json(snap / "status.json", {}) or {}
    fresh = (status.get("run_ct") or "")[:10] == day
    if not dec.exists() and tip:
        if now.strftime("%H:%M") < tip and fresh:
            res["decided"] = bool(decide_day(snap, day, lab, "live", {"run_ct": status.get("run_ct")}))
        elif now.strftime("%H:%M") >= tip:
            res["decided"] = bool(decide_day(snap, day, lab, "live", {"run_ct": status.get("run_ct")}, missed=True))
            res["missed"] = True
    score_days(snap, lab)
    rollup(snap, lab)
    summary(snap, lab, now)
    return res


def snapshot_commits(repo):
    out = subprocess.run(["git", "-C", str(repo), "log", "--format=%H %cI %s", "--", "snapshots"],
                         capture_output=True, text=True, check=True).stdout
    res = []
    for line in out.splitlines():
        h, when, msg = line.split(" ", 2)
        if msg.startswith("snapshot"):
            res.append((datetime.fromisoformat(when).astimezone(CT), h))
    return sorted(res)


def materialize(repo, commit, dest):
    data = subprocess.run(["git", "-C", str(repo), "archive", commit, "snapshots"], capture_output=True,
                          check=True).stdout
    import io
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        tf.extractall(dest, filter="data") if sys.version_info >= (3, 12) else tf.extractall(dest)
    return Path(dest) / "snapshots"


def replay(snap, start, end, lab=None, repo=None):
    snap, lab = Path(snap), Path(lab or LAB)
    repo = Path(repo or ROOT)
    commits = snapshot_commits(repo)
    d, stop, log = date.fromisoformat(start), date.fromisoformat(end), []
    while d <= stop:
        day = d.isoformat()
        tip = first_tip(snap, day)
        if tip:
            cut = datetime.fromisoformat(f"{day}T{tip}").replace(tzinfo=CT)
            before = [c for c in commits if c[0] < cut]
            if before:
                when, h = before[-1]
                tmp = Path(tempfile.mkdtemp(prefix=f"labsnap-{day}-"))
                try:
                    s_dir = materialize(repo, h, tmp)
                    score_days(snap, lab)
                    rec = decide_day(s_dir, day, lab, "backtest",
                                     {"commit": h[:7], "committed_ct": when.isoformat(timespec="minutes")})
                    log.append({"date": day, "commit": h[:7], "committed_ct": when.strftime("%m-%d %H:%M"),
                                "first_tip": tip, "decided": bool(rec)})
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)
        d += timedelta(days=1)
    score_days(snap, lab)
    rollup(snap, lab)
    summary(snap, lab)
    return log


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m relay.lab")
    ap.add_argument("cmd", choices=["daily", "replay", "summary"])
    ap.add_argument("snap")
    ap.add_argument("--ledger", default=str(LAB))
    ap.add_argument("--now", help="Central time override, e.g. 2026-10-28T06:40 (tests)")
    ap.add_argument("--from", dest="start")
    ap.add_argument("--to", dest="end")
    a = ap.parse_args(argv)
    now = datetime.fromisoformat(a.now).replace(tzinfo=CT) if a.now else None
    if a.cmd == "daily":
        print(json.dumps(daily(a.snap, a.ledger, now)))
    elif a.cmd == "replay":
        end = a.end or (datetime.now(CT).date() - timedelta(days=1)).isoformat()
        for row in replay(a.snap, a.start or "2026-10-20", end, a.ledger):
            print(json.dumps(row))
    else:
        rollup(a.snap, a.ledger)
        print(json.dumps(summary(a.snap, a.ledger, now), indent=1))


if __name__ == "__main__":
    main()
