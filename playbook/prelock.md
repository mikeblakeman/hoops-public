# Pre-lock check (daily, late afternoon Central)

Follow `playbook/common.md` first.

1. ArtifactData get `brief/latest` on the brief page (note its version).
2. Fresh pre-lock build: `~/out/prelock.json` (from `prebuilt/brief_prelock.json` with names restored, see
   common.md 3b; built by the newest data run with the `daily` plan). If `brief/latest` used another
   `week.plan_used`, keep the morning's actions and week card and use the pre-lock build only for injuries and the
   lineup. (Interactive sessions: `python tools/brief.py snapshots out/prelock.json --kind prelock --no-log
   --teams ~/teams.json --plan <plan_used>`.)
3. Search for late news on the user's starters with games tonight (ruled out, minutes limit, rest) and on the
   recommended pickups.
3b. Check the week plan in the rebuilt brief: if tomorrow has a planned add, confirm the player is healthy and note it
   so Mike can add him in the morning (adds should be made before the first tip). If today's planned add was not made
   and the player tips later tonight, say so.
3c. IR check: confirm every player in `lineup.ir` is still out. If one is healthy now (or the pre-lock build has an
   `activate` action the morning did not), the first note is "Drop X, then move Y off IR" (the pre-lock build names
   X). ESPN blocks every add until that is done, so never put an add ahead of it.
4. Compare with the morning brief. Write `prelock = {checked_ct, notes[]}` with only what changed: a starter now out
   and who replaces him, a better pickup for tonight, a bench swap. If nothing changed, notes = [].
   If the lineup changed, also replace `lineup` and `actions` with the pre-lock versions.
5. ArtifactData update `brief/latest` (pinned with `if_version`) and set `briefs/<date>-prelock`.
6. End with a one-line summary.
