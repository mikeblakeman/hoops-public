# On-demand questions

Follow `playbook/common.md` first.

- Who to start / what to do today: `python tools/brief.py snapshots out/brief.json --no-log --teams ~/teams.json`;
  check news for any starter listed as day-to-day.
- Pickups: `snapshots/brief_data.json` moves and alternatives, plus `snapshots/valuations.csv` (free agents by
  `vorp_pg`, `team_games_this_matchup`). Explain in categories.
- Trades: rest-of-season value (`vorp_season`), category fit, games during fantasy playoff weeks
  (`schedule_grid.csv`), next-season keeper value (age in `player_bio.csv`).
