# Draft day (keepers lock one hour before the draft)

Follow `playbook/common.md` first (the data job must have run after the keeper lock; if `snapshots/status.json`
is older than the lock, ask the user to wait for the next run or to trigger one).

1. Build the keeper map `{player_id: team_id}` from `snapshots/draft_picks.csv` rows with `keeper` true (or from
   `snapshots/rosters.csv` if picks are not filled yet).
2. Write it to the draft board page database: ArtifactData set `draft/keepers` = `{"ids": {...}}`. The board then
   hides kept players and recomputes fit and availability.
3. Give the user the exact first pick and the plan for the next pair, using the board's fit and "likely there"
   numbers (compute with the same logic from `snapshots/valuations.csv` if needed).
4. During the draft, read `draft/state` from the board to see picks so far when asked.
