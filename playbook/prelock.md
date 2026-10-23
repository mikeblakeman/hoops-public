# Pre-lock check (daily, late afternoon Central)

Follow `playbook/common.md` first.

1. ArtifactData get `brief/latest` on the brief page (note its version).
2. Rebuild with the same stream plan the morning brief used (`week.plan_used` in `brief/latest`; default `low_effort`):
   `python tools/brief.py snapshots out/prelock.json --kind prelock --no-log --teams ~/teams.json --plan <plan_used>`.
3. Search for late news on the user's starters with games tonight (ruled out, minutes limit, rest) and on the
   recommended pickups.
3b. Check the week plan in the rebuilt brief: if tomorrow has a planned add, confirm the player is healthy and note it
   so Mike can add him in the morning (adds should be made before the first tip). If today's planned add was not made
   and the player tips later tonight, say so.
4. Compare with the morning brief. Write `prelock = {checked_ct, notes[]}` with only what changed: a starter now out
   and who replaces him, a better pickup for tonight, a bench swap. If nothing changed, notes = [].
   If the lineup changed, also replace `lineup` and `actions` with the pre-lock versions.
5. ArtifactData update `brief/latest` (pinned with `if_version`) and set `briefs/<date>-prelock`.
6. End with a one-line summary.
