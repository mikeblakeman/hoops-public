"""Build the public data mirror: everything the scheduled brief needs, with no
team names, owner handles, league name or league id.

Usage: python tools/publish_public.py snapshots public_out

Copies code (relay/, tools/), the public playbooks (public/), sanitized
snapshots, and the graded ledger summary. Team ids stay; names are restored
inside Claude from a private project doc.
"""
import csv
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAFE_CSV = ["valuations.csv", "players.csv", "rosters.csv", "matchups.csv", "schedule.csv",
            "daily_slate.csv", "schedule_grid.csv", "matchup_calendar.csv", "matchup_periods.csv",
            "scoring_period_dates.csv", "injuries.csv", "news.csv", "draft_picks.csv", "player_bio.csv"]
SAFE_JSON = ["current_scores.json", "valuation_meta.json", "draft_meta.json"]
TEAM_KEEP = ["team_id", "is_me", "wins", "losses", "ties", "points_for", "points_against", "playoff_seed",
             "waiver_rank", "acquisitions", "drops", "trades", "acq_this_matchup"]


def scrub(text, secrets):
    for s in secrets:
        if s:
            text = text.replace(str(s), "[hidden]")
    return text


def main(snap, out):
    snap, out = Path(snap), Path(out)
    status = json.loads((snap / "status.json").read_text())
    settings = json.loads((snap / "league_settings.json").read_text())
    teams = list(csv.DictReader((snap / "teams.csv").open())) if (snap / "teams.csv").exists() else []
    secrets = [status.get("league_id"), settings.get("league_name", "").strip(), status.get("my_team")]
    for t in teams:
        secrets += [t.get("name")] + [o.strip() for o in (t.get("owners") or "").split(";")]
    secrets = sorted({s for s in secrets if s and len(str(s)) >= 3}, key=lambda s: -len(str(s)))

    if out.exists():
        for child in out.iterdir():
            if child.name != ".git":
                shutil.rmtree(child) if child.is_dir() else child.unlink()
    out.mkdir(parents=True, exist_ok=True)
    s_out = out / "snapshots"
    s_out.mkdir()

    for name in SAFE_CSV:
        if (snap / name).exists():
            (s_out / name).write_text(scrub((snap / name).read_text(), secrets))
    for name in SAFE_JSON:
        if (snap / name).exists():
            (s_out / name).write_text(scrub((snap / name).read_text(), secrets))
    if teams:
        with (s_out / "teams.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=TEAM_KEEP, extrasaction="ignore")
            w.writeheader()
            w.writerows(teams)
    st = {k: status.get(k) for k in ("run_utc", "run_ct", "season", "ok", "steps", "warnings",
                                     "current_scoring_period", "current_matchup_period", "my_team_id",
                                     "counts_players")}
    st["errors"] = [scrub(e, secrets) for e in status.get("errors", [])]
    (s_out / "status.json").write_text(json.dumps(st, indent=2))
    lg = dict(settings)
    lg["league_name"] = "[hidden]"
    (s_out / "league_settings.json").write_text(json.dumps(lg, indent=2))
    if (snap / "brief_data.json").exists():
        bd = json.loads((snap / "brief_data.json").read_text())
        bd["me"]["name"] = "My team"
        bd["opponent"]["name"] = f"Team {bd['opponent'].get('team_id')}"
        bd["opponent"]["owners"] = ""
        (s_out / "brief_data.json").write_text(scrub(json.dumps(bd, indent=1), secrets))
    hist = snap / "history"
    if hist.exists():
        shutil.copytree(hist, s_out / "history")

    # code and public playbooks
    for d in ("relay", "tools"):
        shutil.copytree(ROOT / d, out / d, ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", "README.md", "board_template.html", "build_board.py", ".trigger"))
    shutil.copytree(ROOT / "public" / "playbook", out / "playbook")
    shutil.copy(ROOT / "public" / "README.md", out / "README.md")
    # graded ledger (decisions are logged and graded by the private relay)
    led = ROOT / "ledger"
    (out / "ledger").mkdir()
    for name in ("summary.json", "graded_decisions.csv"):
        if (led / name).exists():
            (out / "ledger" / name).write_text(scrub((led / name).read_text(), secrets))

    # final check: nothing identifying slipped through
    leaks = []
    for fp in out.rglob("*"):
        if fp.is_file() and ".git" not in fp.parts:
            txt = fp.read_text(errors="ignore")
            for s in secrets:
                if re.search(re.escape(str(s)), txt):
                    leaks.append(f"{fp.relative_to(out)}: {s!r}")
    if leaks:
        print("LEAKS:\n" + "\n".join(leaks[:20]))
        sys.exit(1)
    print(f"public mirror built at {out} ({sum(1 for _ in out.rglob('*') if _.is_file())} files, no leaks)")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
