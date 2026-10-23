# Morning brief (daily, ready by 8:00 AM Central)

Follow `playbook/common.md` first.

1. Build: `python tools/brief.py snapshots out/brief.json --kind morning --no-log --teams ~/teams.json`
   The actions and the page's Week ahead card must show ONE stream plan. `brief.py` uses `daily` (add on every day
   the gain is worth it; Mike's choice, 2026-10-22) unless you pass `--plan low_effort` (few adds, then hold); the doc's `week.plan_used` records which, and
   the card draws that plan. Pick the plan when you build. Never hand-write actions from the other plan (that is how
   the top section and the card once named different players). If the data run is a day old, brief.py rebuilds the
   week plan for today by itself (`week.plan_date` shows the day it was built for).
1b. Week plan: `out/brief.json` has a `week` block (day-by-day slot coverage, the streaming-spot plan, trade ideas) built
   by `relay/weekplan.py`. The action marked "Week plan" is today's call for the streaming spot; the "Plan ahead"
   action lists the later adds and the light days. The plan replaces the engine's single-day swap moves. Plan adds are
   made in the morning, before the day's first tip. Do not invent trades: only report `week.trades`.
2. News check (about 5 searches): late injury, rest and lineup news for the user's players with games today, the
   top recommended pickups (today's planned add and the next day's), and the opponent's key players. If a planned
   player is hurt or his minutes are cut, use one of the step's `alternatives` and say why, and add a `notes` line
   naming the player the card still shows so the two sections do not look contradictory. If news changes a recommendation (a starter ruled out,
   a pickup's minutes cut), edit `actions` and say why.
3. Expert calls (about 3 searches): today's public fantasy basketball waiver, streaming and start/sit articles (for
   example ESPN, CBS, Yahoo, NBC Sports, FantasyPros, Hashtag Basketball, RotoBaller, Basketball Monster).
   Collect up to 15 specific calls: `{date, source, url, player, call, horizon_days, note}` where call is one of
   add, stream, start, sit, drop, buy, sell, hold; horizon 1 for tonight-only calls, 7 for weekly adds, 14 for
   buy/sell. Save them to the brief page database: ArtifactData set `calls/<date>` = `{"date": ..., "calls": [...]}`.
   Put up to 6 calls that matter to today's decisions in the brief's `experts` (`source, url, player, call, note`).
4. Judgment fields in `out/brief.json`:
   - `headline`: one sentence on what matters most today (mention any game that tips before 6 PM Central).
   - `summary`: two or three sentences on the matchup state and the plan, including any light day ahead (a day with 4+
     empty slots) and the next planned add.
   - Mondays and the first day of a matchup: add one note with the best `week.trades` idea (partner, who goes, who comes,
     cats per week for each side, ages). Trades are Mike's call; never imply they are urgent.
   - `news`: `[{player, text, source, url}]`, only items that affect a decision.
   - `notes`: keep any line that starts with "Data warning" (the data run behind the brief is old); say so in the summary too.
5. Mondays (or the first day of a new matchup): weekly review. Skip it, and leave `weekly` null, until a matchup
   has finished (the brief's `matchup_period` is 2 or later and `ledger/summary.json` exists).
   - Decisions: read `ledger/summary.json` (graded by the data job): `add_win_rate`, `adds_graded`, `misses`.
   - Expert calls: export the page database collection `calls` (ArtifactData list with `out_dir` ~/calls), then run
     `python tools/grade.py snapshots --ledger ~/ledger --calls-dir ~/calls` and read `~/ledger/summary.json` `sources`.
   - Fill `weekly`: `result` (last matchup's category score), `add_win_rate`, `adds_graded`, `misses`, `sources`,
     and 1 to 3 `lessons` (patterns only, never one-off outcomes). Append one line summarizing the week to the
     project doc `claude/log/decisions.md`.
6. Publish: ArtifactData batch on the brief page: set `brief/latest` and set `briefs/<date>-morning` from
   `out/brief.json` (read `brief/latest` first to get its version for `if_version`).
7. End with a two-line summary.
