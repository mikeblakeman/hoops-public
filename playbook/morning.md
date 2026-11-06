# Morning brief (daily, ready by 8:00 AM Central)

Follow `playbook/common.md` first.

1. Brief: `~/out/morning.json` (from `prebuilt/brief_morning.json` with team names restored, see common.md 3b).
   Its `date` must be today; `prebuilt.data_run_ct` says which data run it used (say so if it is old). Do not
   rebuild it by hand unless `prebuilt/status.json` reports an error for `morning` or the date is not today.
   (Interactive sessions: `python tools/brief.py snapshots out/brief.json --kind morning --no-log --teams ~/teams.json`.)
   The actions and the page's Week ahead card must show ONE stream plan. `brief.py` uses `daily` (add on every day
   the gain is worth it; Mike's choice, 2026-10-22) unless you pass `--plan low_effort` (few adds, then hold); the doc's `week.plan_used` records which, and
   the card draws that plan. Pick the plan when you build. Never hand-write actions from the other plan (that is how
   the top section and the card once named different players). If the newest data run is from an earlier day,
   brief.py rebuilds EVERY section (lineup, odds, moves, week plan) for today from that data and adds a "Data warning"
   note; do not add your own notes about stale cards. A planned swap that adds nothing on its first day is moved to
   the day it starts paying off.
1b. Week plan: `out/brief.json` has a `week` block (day-by-day slot coverage, the streaming-spot plan, trade ideas) built
   by `relay/weekplan.py`. The action marked "Week plan" is today's call for the streaming spot; the "Plan ahead"
   action lists the later adds and the light days. The plan replaces the engine's single-day swap moves. Plan adds are
   made in the morning, before the day's first tip. Do not invent trades: only report `week.trades`.
1c. Next opponent (last 3 days of a matchup only): `out/brief.json` has a `next_opponent` block (forecast vs next
   week's opponent, games by day, how they have played, their key players and injuries) and the page shows it as the
   "Next opponent" card. Rule (Mike, 2026-10-24): this week comes first. The only next-week move is the streaming
   spot, and the planner already wrote it into the stream plan (days marked `carry`), so the actions, Week ahead card
   and this card agree. Never suggest any other move for next week (drops, trades, lineup) in notes or the summary.
   If news changes a carry add, edit the plan day as in step 2 and update `next_opponent.carry` to match: change
   `end_holder`, `end_holder_id` and `monday_holder` to the new end-of-week streamer and keep `changed: false`.
   Set `changed: true` only when you also fill `player`, `team`, `pos`, `from_date`, `this_week_cost`,
   `next_week_gain` and `replaces` (the page reads them; on Oct 25 a bare `changed: true` printed "add () on
   Invalid Date" and "NaN"). Do not mention
   the card on other days; `next_opponent` is null then.
2. News check (about 5 searches): late injury, rest and lineup news for the user's players with games today, the
   top recommended pickups (today's planned add and the next day's), and the opponent's key players. If a planned
   player is hurt or his minutes are cut, use one of the step's `alternatives` (or hold the current streamer) and say
   why. Edit BOTH the action and that day in `week.stream_plan.plans[<plan_used>].days` (`occupant`, `team`, `drop`,
   `swap`) so the Week ahead card shows the same players; never leave a note explaining a mismatch. If news changes
   any other recommendation (a starter ruled out), edit `actions` and `lineup` together and say why.
   IR moves: an `ir` action is only valid while ESPN shows the player Out. Check his current status in the news
   check (rest-night designations often flip to day-to-day by morning). If he is day-to-day, remove the IR action
   and the open-spot add that depends on it, and fix `lineup` to match.
   The lineup card shows today's lineup AFTER the recommended moves (`lineup.after_moves` lists them). If you change
   or cancel a move, take the matching ready-made card from `lineup_options` and set it as `lineup`: the key is the
   alternative's player id (switch to that alternative) or `hold:<planned player id>` (skip the planned add). If no
   option fits, edit `lineup` by hand and say so in the notes.
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
4b. Strategy lab: `out/brief.json` already has `lab` from the data job, and `strategy` says which strategy the actions
   follow (`relay/strategy.json`, Mike approves every switch). If `lab.ready` is set, brief.py already put the "Ready
   to switch" action at the top; mention it in the summary. If `lab.switch_back` is set, the "Switch back?" action is
   at the top; mention that too. Never switch strategies on your own. Mondays: add one line to the weekly review with
   each strategy's edge and status. When the active strategy is not `current`, the actions come from that strategy
   and the Week ahead card shows no streaming plan; if news changes one of its moves, edit `actions` and `lineup`
   together as in step 2.
5. Mondays (or the first day of a new matchup): weekly review. Skip it, and leave `weekly` null, until a matchup
   has finished (the brief's `matchup_period` is 2 or later and `ledger/summary.json` exists).
   - Decisions: read `ledger/summary.json` (graded by the data job): `add_win_rate`, `adds_graded`, `misses`.
   - Expert calls: export the page database collection `calls` (ArtifactData list with `out_dir` ~/calls). Grade
     each call whose horizon has finished by looking it up in `prebuilt/call_values.csv` (player by name or id,
     `start` = the call's date, `horizon_days` = its horizon) and applying the rule in
     `prebuilt/call_values_meta.json`; then build `sources` per source: `n`, `hits`, `shrunk_rate`, `trust`.
     A small script you write yourself for this is fine; do not run `tools/grade.py` in a scheduled run.
   - Fill `weekly`: `result` (last matchup's category score), `add_win_rate`, `adds_graded`, `misses`, `sources`,
     and 1 to 3 `lessons` (patterns only, never one-off outcomes). Append one line summarizing the week to the
     project doc `claude/log/decisions.md`.
6. Publish: ArtifactData batch on the brief page: set `brief/latest` and set `briefs/<date>-morning` from
   `~/out/morning.json` (read `brief/latest` first to get its version for `if_version`). The extra keys
   `prebuilt`, `move_ids` and `lineup_options` can stay; the page ignores them.
7. End with a two-line summary.
