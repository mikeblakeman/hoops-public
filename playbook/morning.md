# Morning brief (daily, ready by 8:00 AM Central)

Follow `playbook/common.md` first.

1. Build: `python tools/brief.py snapshots out/brief.json --kind morning --no-log --teams ~/teams.json`
2. News check (about 5 searches): late injury, rest and lineup news for the user's players with games today, the
   top recommended pickups, and the opponent's key players. If news changes a recommendation (a starter ruled out,
   a pickup's minutes cut), edit `actions` and say why.
3. Expert calls (about 3 searches): today's public fantasy basketball waiver, streaming and start/sit articles (for
   example ESPN, CBS, Yahoo, NBC Sports, FantasyPros, Hashtag Basketball, RotoBaller, Basketball Monster).
   Collect up to 15 specific calls: `{date, source, url, player, call, horizon_days, note}` where call is one of
   add, stream, start, sit, drop, buy, sell, hold; horizon 1 for tonight-only calls, 7 for weekly adds, 14 for
   buy/sell. Save them to the brief page database: ArtifactData set `calls/<date>` = `{"date": ..., "calls": [...]}`.
   Put up to 6 calls that matter to today's decisions in the brief's `experts` (`source, url, player, call, note`).
4. Judgment fields in `out/brief.json`:
   - `headline`: one sentence on what matters most today (mention any game that tips before 6 PM Central).
   - `summary`: two or three sentences on the matchup state and the plan.
   - `news`: `[{player, text, source, url}]`, only items that affect a decision.
5. Mondays (or the first day of a new matchup): weekly review.
   - Decisions: read `ledger/summary.json` (graded by the data job): `add_win_rate`, `adds_graded`, `misses`.
   - Expert calls: export the page database collection `calls` (ArtifactData list with `out_dir` ~/calls), then run
     `python tools/grade.py snapshots --ledger ~/ledger --calls-dir ~/calls` and read `~/ledger/summary.json` `sources`.
   - Fill `weekly`: `result` (last matchup's category score), `add_win_rate`, `adds_graded`, `misses`, `sources`,
     and 1 to 3 `lessons` (patterns only, never one-off outcomes). Append one line summarizing the week to the
     project doc `claude/log/decisions.md`.
6. Publish: ArtifactData batch on the brief page: set `brief/latest` and set `briefs/<date>-morning` from
   `out/brief.json` (read `brief/latest` first to get its version for `if_version`).
7. End with a two-line summary.
