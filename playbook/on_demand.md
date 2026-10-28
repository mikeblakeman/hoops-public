# On-demand questions

Follow `playbook/common.md` first.

- Who to start / what to do today: `python tools/brief.py snapshots out/brief.json --no-log --teams ~/teams.json`;
  check news for any starter listed as day-to-day.
- Week planning: `snapshots/brief_data.json` `week_plan` has slot coverage for every remaining day, the day-by-day
  streaming plan (`stream_plan.plans.daily` and `low_effort`), a second-spot test and trade ideas. Use it for "what
  should I do the rest of the week" questions; run `python -m relay.weekplan snapshots [YYYY-MM-DD]` to refresh it.
- Next opponent (last 3 days of a matchup): `week_plan.next_opponent` in brief_data.json (forecast, games by day,
  their form, roster and injuries). Its streaming advice lives in the stream plan (`carry`); answer next-week questions
  from it and never recommend a next-week move that costs this week more than `carry.max_cost`.
- Pickups: `snapshots/brief_data.json` `week_plan` first, then moves and alternatives, plus `snapshots/valuations.csv` (free agents by
  `vorp_pg`, `team_games_this_matchup`). Explain in categories.
- Trades: start from `week_plan.trades.top` (cats per week for both sides), then check rest-of-season value (`vorp_season`), category fit, games during fantasy playoff weeks
  (`schedule_grid.csv`), next-season keeper value (age in `player_bio.csv`).
- Strategy questions ("how is fill every slot doing?"): read `ledger/lab/summary.json` (and `ledger/lab/weeks.csv` in
  hoops-relay); explain edge, weeks, live weeks and confidence in plain words. The paper teams are simulated; say so.
- "Switch to <id>" from Mike: if `lab.ready.id` is not that id, say what is missing (weeks, live weeks, edge,
  confidence) and ask once more; if he confirms, switch anyway, since it is his call. To switch (session with push access to
  hoops-relay): set `relay/strategy.json` to `{"active": "<id>", "since": "<today>", "approved_by": "Mike",
  "previous": "<old active>"}`, run `python -m unittest discover -s tests -t .`, push, confirm the next data run is
  green and that `python tools/brief.py snapshots out/brief.json --no-log` shows `strategy.followed == "<id>"`, then
  log the switch in `claude/log/decisions.md` with the evidence. "Switch back" is the same edit with the ids swapped.
