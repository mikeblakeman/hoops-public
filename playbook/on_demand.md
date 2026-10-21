# On-demand questions

Follow `playbook/common.md` first.

- Who to start / what to do today: `python tools/brief.py snapshots out/brief.json --no-log --teams ~/teams.json`;
  check news for any starter listed as day-to-day.
- Week planning: `snapshots/brief_data.json` `week_plan` has slot coverage for every remaining day, the day-by-day
  streaming plan (`stream_plan.plans.daily` and `low_effort`), a second-spot test and trade ideas. Use it for "what
  should I do the rest of the week" questions; run `python -m relay.weekplan snapshots [YYYY-MM-DD]` to refresh it.
- Pickups: `snapshots/brief_data.json` `week_plan` first, then moves and alternatives, plus `snapshots/valuations.csv` (free agents by
  `vorp_pg`, `team_games_this_matchup`). Explain in categories.
- Trades: start from `week_plan.trades.top` (cats per week for both sides), then check rest-of-season value (`vorp_season`), category fit, games during fantasy playoff weeks
  (`schedule_grid.csv`), next-season keeper value (age in `player_bio.csv`).
