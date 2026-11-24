"""Trades accepted in ESPN but still in the review period, and trade offers waiting on an answer.

ESPN holds an accepted trade for the league's review period (48 hours here; 3 veto votes cancel it) and only
then moves the players. Until then the data feed shows the old rosters, so a brief built on it plans around
players who are about to leave. This module finds such trades and lets the brief plan as if they had gone
through from the moment they are due.

Sources, merged by trade id:
  1. ESPN, written by the data job to snapshots/pending_trades.json (`extract_espn`, best effort: ESPN's
     pending-transaction format is not documented, so the parser is tolerant and the raw shape is saved to
     debug/pending_probe.json for checking).
  2. A file the chat keeps (project doc claude/league/pending-trades.json), passed to tools/brief.py with
     --pending, in the same format:
     {"trades": [{"id": "...", "accepted_ct": "2026-11-02T14:30-06:00", "process_ct": "2026-11-04T14:30-06:00",
                  "out": ["6442", "4683021"], "in": ["6583"], "partner_team_id": "3", "note": "..."}]}
     `out` leave Mike's team, `in` arrive. process_ct may be left out: accepted_ct + the league's review hours.

Offers are not trades. ESPN lists a proposal nobody has accepted yet (type TRADE_PROPOSAL, status PENDING) next to
accepted trades in review (type TRADE_ACCEPT, with acceptedDate and processDate). Until 2026-11-24 both were treated
as accepted, so a brief planned around losing players in an offer Mike had not answered (an incoming offer for his
best player). `extract_espn` now tags each entry with `kind`: "accepted", "offer_received" (another team proposed it
to Mike; `proposer_team_id`) or "offer_sent" (Mike proposed it). Only accepted trades go in "trades"; offers go in
"offers" and never change a roster. relay/weekplan.py trade_search scores each offer and gives a verdict.

A trade is active only while every `out` player is still on Mike's team and every `in` player is still on the
partner's team in rosters.csv. Once ESPN processes it (or a trade is canceled and the players move elsewhere)
it drops out by itself. A trade 12+ hours past its process time that has still not moved is reported as overdue
and ignored (probably vetoed or canceled).
"""
import csv
import json
import shutil
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
DONE = {"EXECUTED", "CANCELED", "CANCELLED", "FAILED", "VETOED", "REJECTED", "DECLINED", "EXPIRED"}
OVERDUE_HOURS = 12
ACCEPTED, OFFER_RECEIVED, OFFER_SENT = "accepted", "offer_received", "offer_sent"
OFFERS = (OFFER_RECEIVED, OFFER_SENT)


def kind_of(tx, my_tid):
    """accepted | offer_received | offer_sent. ESPN: TRADE_PROPOSAL = proposed, not yet accepted; its teamId is the
    proposing team. Any other trade type (TRADE_ACCEPT, or an older shape without a type suffix) is an accepted trade
    in review."""
    ttype = str(tx.get("type") or "").upper()
    if "PROPOSAL" not in ttype:
        return ACCEPTED
    return OFFER_SENT if str(tx.get("teamId")) == str(my_tid) else OFFER_RECEIVED


def is_offer(t):
    return (t or {}).get("kind") in OFFERS


def _rows(p):
    p = Path(p)
    if not p.exists() or p.stat().st_size == 0:
        return []
    with p.open() as fh:
        return list(csv.DictReader(fh))


def _dt(x):
    if not x:
        return None
    if isinstance(x, (int, float)):
        return datetime.fromtimestamp(x / 1000, CT)
    try:
        d = datetime.fromisoformat(str(x))
        return d if d.tzinfo else d.replace(tzinfo=CT)
    except ValueError:
        return None


def _iso(d):
    return d.astimezone(CT).isoformat(timespec="minutes") if d else ""


# ------------------------------------------------------------------ ESPN (data job)
def _walk(obj, out, path=""):
    """Every dict that looks like a transaction with player items: {"items": [{"playerId", "fromTeamId", ...}]}."""
    if isinstance(obj, dict):
        items = obj.get("items")
        if isinstance(items, list) and items and all(isinstance(i, dict) and "playerId" in i for i in items):
            out.append((path, obj))
        for k, v in obj.items():
            _walk(v, out, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, out, path + "[]")


def extract_espn(payloads, my_tid, review_hours, seen=None, now=None):
    """Pending trades involving my team from ESPN JSON payloads (any views). `seen`: {tx_id: first-seen ISO},
    updated in place, used when ESPN gives no accepted or process date. Returns (trades, probe)."""
    now = now or datetime.now(CT)
    seen = seen if seen is not None else {}
    found, probe = {}, {"paths": [], "sample": None}
    for payload in payloads:
        hits = []
        _walk(payload, hits)
        for path, tx in hits:
            if path not in probe["paths"]:
                probe["paths"].append(path)
            ttype = str(tx.get("type") or "").upper()
            status = str(tx.get("status") or "").upper()
            moves = [(str(i.get("playerId")), str(i.get("fromTeamId")), str(i.get("toTeamId")))
                     for i in tx["items"] if i.get("fromTeamId") is not None and i.get("toTeamId") is not None]
            if "TRADE" not in ttype or status in DONE or not moves:
                continue
            teams = {f for _, f, _ in moves} | {t for _, _, t in moves}
            if str(my_tid) not in teams:
                continue
            tid = str(tx.get("id") or "|".join(sorted(p for p, _, _ in moves)))
            if probe["sample"] is None:
                probe["sample"] = {k: (v if k != "items" else v[:4]) for k, v in tx.items()
                                   if not isinstance(v, (dict, list)) or k == "items"}
            dates = {k: _dt(v) for k, v in tx.items() if k.lower().endswith("date") and v}
            seen.setdefault(tid, _iso(now))
            kind = kind_of(tx, my_tid)
            row = {
                "id": tid, "source": "espn", "status": status or None, "kind": kind,
                "out": [p for p, f, _ in moves if f == str(my_tid)],
                "in": [p for p, _, t in moves if t == str(my_tid)],
                "partner_team_id": next((t for t in teams if t != str(my_tid)), None),
            }
            if kind == ACCEPTED:
                accepted = dates.get("acceptedDate") or dates.get("proposedDate") or _dt(seen[tid])
                process = dates.get("processDate") or (
                    accepted + timedelta(hours=review_hours or 48) if accepted else None)
                row.update(accepted_ct=_iso(accepted), process_ct=_iso(process))
            else:   # an offer: nothing moves unless it is accepted; ESPN's expirationDate is when the offer lapses
                row.update(proposer_team_id=str(tx.get("teamId")) if tx.get("teamId") is not None else None,
                           proposed_ct=_iso(dates.get("proposedDate") or _dt(seen[tid])),
                           expires_ct=_iso(dates.get("expirationDate")))
            found[tid] = row
    return list(found.values()), probe


def split(entries):
    """(accepted trades, offers)."""
    return [t for t in entries if not is_offer(t)], [t for t in entries if is_offer(t)]


# ------------------------------------------------------------------ brief side
def load(snap, extra=None):
    """All known pending trades (snapshot file plus an optional chat-kept file), merged by id."""
    snap = Path(snap)
    out = {}
    for src in (snap / "pending_trades.json", Path(extra) if extra else None):
        if not src or not Path(src).exists():
            continue
        try:
            data = json.loads(Path(src).read_text())
        except ValueError:
            continue
        for t in (data.get("trades") if isinstance(data, dict) else data) or []:
            if (t.get("out") or t.get("in")) and not is_offer(t):   # an offer is not a trade until accepted
                out.setdefault(str(t.get("id") or len(out)), t)
    return list(out.values())


def load_offers(snap, my_tid, now=None):
    """Open trade offers involving Mike (snapshots/pending_trades.json "offers"), still valid: every player is still
    where the offer found him and the offer has not expired. Each gets `names_out` / `names_in`."""
    snap = Path(snap)
    now = now or datetime.now(CT)
    src = snap / "pending_trades.json"
    try:
        data = json.loads(src.read_text()) if src.exists() else {}
    except ValueError:
        data = {}
    entries = (data.get("offers") or []) + [t for t in (data.get("trades") or []) if is_offer(t)]
    owner = {r["player_id"]: str(r["team_id"]) for r in _rows(snap / "rosters.csv")}
    names = {r["player_id"]: r["name"] for r in _rows(snap / "valuations.csv")}
    keep, ids = [], set()
    for t in entries:
        t = dict(t, out=[str(p) for p in t.get("out", [])], **{"in": [str(p) for p in t.get("in", [])]})
        partner = str(t.get("partner_team_id") or "")
        if not t["out"] and not t["in"] or t.get("id") in ids:
            continue
        if not all(owner.get(p) == str(my_tid) for p in t["out"]) or not all(owner.get(p) == partner for p in t["in"]):
            continue
        exp = _dt(t.get("expires_ct"))
        if exp and now > exp:
            continue
        t["names_out"] = [names.get(p, p) for p in t["out"]]
        t["names_in"] = [names.get(p, p) for p in t["in"]]
        ids.add(t.get("id"))
        keep.append(t)
    return keep


def active(snap, trades, my_tid, review_hours=48, now=None):
    """(active trades, notes). Active = players have not moved yet and the trade is not overdue."""
    now = now or datetime.now(CT)
    owner = {r["player_id"]: str(r["team_id"]) for r in _rows(Path(snap) / "rosters.csv")}
    names = {r["player_id"]: r["name"] for r in _rows(Path(snap) / "valuations.csv")}
    keep, notes = [], []
    for t in trades:
        t = dict(t, out=[str(p) for p in t.get("out", [])], **{"in": [str(p) for p in t.get("in", [])]})
        partner = str(t.get("partner_team_id") or next((owner.get(p) for p in t["in"] if owner.get(p)), ""))
        t["partner_team_id"] = partner
        if not all(owner.get(p) == str(my_tid) for p in t["out"]) or not all(owner.get(p) == partner for p in t["in"]):
            continue                                   # processed already, or the players went elsewhere
        proc = _dt(t.get("process_ct")) or (
            _dt(t.get("accepted_ct")) + timedelta(hours=review_hours) if _dt(t.get("accepted_ct")) else None)
        t["process_ct"] = _iso(proc)
        t["names_out"] = [names.get(p, p) for p in t["out"]]
        t["names_in"] = [names.get(p, p) for p in t["in"]]
        if proc and now > proc + timedelta(hours=OVERDUE_HOURS):
            notes.append(f"Trade ({describe(t)}) was due to process {proc.strftime('%a %-I:%M %p')} CT but ESPN "
                         "still shows the old rosters. It may have been vetoed or canceled; check ESPN. This brief "
                         "uses the current roster.")
            continue
        keep.append(t)
    return keep, notes


def describe(t):
    return f"{' + '.join(t['names_out']) or 'nothing'} for {' + '.join(t['names_in']) or 'nothing'}"


def first_tips(snap):
    """{date: datetime of the day's first tip, Central}."""
    tips = {}
    for g in _rows(Path(snap) / "schedule.csv"):
        d, tip = g.get("date_ct"), g.get("tip_ct")
        if not d or not tip:
            continue
        try:
            dt = datetime.fromisoformat(f"{d}T{tip}").replace(tzinfo=CT)
        except ValueError:
            continue
        if d not in tips or dt < tips[d]:
            tips[d] = dt
    return tips


def effect_day(snap, trades, now=None):
    """First game date the trades count for, and whether that is the next game day (the whole brief can be built
    as if processed) or a later one. Unknown process time counts as processed by the next game day."""
    now = now or datetime.now(CT)
    tips = first_tips(snap)
    upcoming = sorted((d, t) for d, t in tips.items() if t > now)
    if not upcoming:
        return None, "now"
    proc = max((_dt(t.get("process_ct")) for t in trades if _dt(t.get("process_ct"))), default=None)
    if proc is None or proc <= upcoming[0][1]:
        return upcoming[0][0], "now"
    for d, tip in upcoming:
        if tip >= proc:
            return d, "later"
    return None, "after_period"


def materialize(snap, trades, my_tid):
    """A copy of the snapshot with the trades applied to rosters.csv and valuations.csv (history is linked, not
    copied). Returns the new snapshot path; the caller rebuilds brief_data.json in it."""
    snap = Path(snap)
    tmp = Path(tempfile.mkdtemp(prefix="pending_")) / "snapshots"
    tmp.mkdir()
    for p in snap.iterdir():
        if p.name in ("history", "debug", "preseason"):
            (tmp / p.name).symlink_to(p.resolve(), target_is_directory=True)
        elif p.is_dir():
            shutil.copytree(p, tmp / p.name)
        else:
            shutil.copy2(p, tmp / p.name)
    move = {}
    for t in trades:
        for pid in t["out"]:
            move[pid] = str(t["partner_team_id"])
        for pid in t["in"]:
            move[pid] = str(my_tid)
    for name, team_col in (("rosters.csv", "team_id"), ("valuations.csv", "on_team_id")):
        rows = _rows(tmp / name)
        if not rows:
            continue
        cols = list(rows[0].keys())
        for r in rows:
            if r.get("player_id") in move:
                r[team_col] = move[r["player_id"]]
                if name == "rosters.csv":
                    r["slot"], r["acquisition_type"] = "BE", "TRADE"
        (tmp / name).unlink()
        with (tmp / name).open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
    return tmp
