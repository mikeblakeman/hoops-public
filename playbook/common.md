# Common setup for scheduled and on-demand runs

1. Get the data (public, no credentials needed): clone the public data repository named in the task prompt,
   `git clone -q --depth 1 <public repo URL> ~/hoops` (or `git -C ~/hoops pull -q`). Work from `~/hoops`.
2. Freshness: read `snapshots/status.json`. Morning job: `run_ct` should be from today after 4:00 AM Central.
   Pre-lock job: after 2:30 PM Central. If stale or `ok` is false, continue with what is there and add a note to
   the brief saying how old the data is. (Only the private data job can refresh it; it runs about 4:15 to 6:20 AM,
   2:45 to 3:55 PM Central.)
3. Team names: teams appear only as ids here. Read the project doc `claude/league/teams.json` with the Projects
   tool, save it as `~/teams.json`, and pass `--teams ~/teams.json` to `tools/brief.py`.
4. Page addresses (brief page, draft board) come from the task prompt. Write to them with the ArtifactData tool.
5. Writing rules for anything the user reads: plain language, Central time, no em dashes, short sentences.
   News and expert items: one-line paraphrase plus a source link; never copy article text.
6. Do not try to push to any repository. Decisions are logged and graded by the private data job; expert calls
   are stored in the brief page database (`calls/<date>`).
