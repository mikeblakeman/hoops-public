"""Pull ESPN league + NBA data and write tidy snapshots.

Usage: python -m relay.fetch --out snapshots
Env:   ESPN_LEAGUE_ID, ESPN_S2, ESPN_SWID, SEASON (ESPN season id, 2027 = 2026-27)
"""
import argparse
import csv
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from . import bio as B
from . import calendar as C
from . import matchup as MU
from . import normalize as N
from . import valuations as V
from .espn_client import EspnAuthError, EspnClient

LEAGUE_VIEWS = ["mSettings", "mTeam", "mRoster", "mMatchup", "mStandings", "mStatus", "mDraftDetail"]


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    cols = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str))


def run(out_dir, client, my_swid):
    out = Path(out_dir)
    now = datetime.now(timezone.utc)
    status = {"run_utc": now.isoformat(timespec="seconds"),
              "run_ct": now.astimezone(N.CT).isoformat(timespec="minutes"),
              "season": client.season, "league_id": client.league_id,
              "ok": False, "steps": {}, "warnings": [], "errors": []}

    def step(name, fn):
        try:
            res = fn()
            status["steps"][name] = "ok"
            return res
        except EspnAuthError:
            raise
        except Exception as e:  # keep going; report in status.json
            status["steps"][name] = "failed"
            status["errors"].append(f"{name}: {type(e).__name__}: {e}")
            status["errors"].append(traceback.format_exc(limit=3))
            return None

    try:
        league = client.league(LEAGUE_VIEWS)
        status["steps"]["league"] = "ok"
    except EspnAuthError as e:
        status["errors"].append(str(e))
        write_json(out / "status.json", status)
        return status

    settings = N.settings_summary(league)
    write_json(out / "league_settings.json", settings)
    sp = settings["status"].get("latest_scoring_period") or 1
    status["current_scoring_period"] = sp
    status["current_matchup_period"] = settings["status"].get("current_matchup_period")

    team_rows = step("teams", lambda: N.teams(league, my_swid))
    if team_rows:
        write_csv(out / "teams.csv", team_rows)
        mine = [t for t in team_rows if t["is_me"]]
        status["my_team"] = mine[0]["name"] if mine else None
        status["my_team_id"] = mine[0]["team_id"] if mine else None
        if not mine:
            status["warnings"].append("could not match SWID to a team owner")
    roster_rows = step("rosters", lambda: N.rosters(league))
    if roster_rows is not None:
        write_csv(out / "rosters.csv", roster_rows)
    mp_rows = step("matchup_periods", lambda: N.matchup_periods(league)) or []
    n_periods = len({r["matchup_period"] for r in mp_rows})
    write_csv(out / "matchups.csv", step("matchups", lambda: N.matchups(league)) or [])
    picks = step("draft", lambda: N.draft_picks(league))
    if picks:
        write_csv(out / "draft_picks.csv", picks[0])
        write_json(out / "draft_meta.json", picks[1])

    # schedule
    sched_rows, sp_dates = [], {}
    sched = step("pro_schedule", client.pro_schedule)
    if sched:
        sched_rows = N.pro_schedule(sched)
        sp_dates, w = N.scoring_period_dates(sched_rows)
        status["warnings"] += w[:10]
        write_csv(out / "schedule.csv", sched_rows)
        slate = N.daily_slate(sched_rows)
        write_csv(out / "daily_slate.csv", slate)
        cal = step("calendar", lambda: C.build(
            league, sp_dates, slate, n_periods,
            settings["schedule"].get("regular_season_matchups") or n_periods))
        if cal and cal[0]:
            write_csv(out / "matchup_calendar.csv", cal[0])
            mp_rows = cal[1]
            if any("unconfirmed" in r["source"] for r in cal[0]):
                status["warnings"].append(
                    "matchup calendar unconfirmed until games start: it assumes the All-Star "
                    "week is the double matchup; it self-corrects from ESPN data by Oct 26 "
                    "(playoff weeks are the same either way)")
        write_csv(out / "scoring_period_dates.csv",
                  [{"scoring_period": k, "date_ct": v} for k, v in sorted(sp_dates.items())])
        if mp_rows:
            write_csv(out / "schedule_grid.csv", N.schedule_grid(sched_rows, mp_rows, sp_dates))

    write_csv(out / "matchup_periods.csv", mp_rows)

    # one-time probe: does ESPN expose matchup period dates anywhere?
    probe_path = out / "debug" / "probe_status.json"
    if not probe_path.exists():
        def probe():
            res = {"schedule_entry_sample": (league.get("schedule") or [{}])[0]}
            for spx in (1, 7, 8, 125, 174):
                d = client.league(["mStatus", "mMatchupScore"], scoring_period=spx)
                sch = d.get("schedule") or []
                res[str(spx)] = {"scoringPeriodId": d.get("scoringPeriodId"),
                                 "status": d.get("status"),
                                 "schedule_len": len(sch),
                                 "schedule_mp_ids": sorted({m.get("matchupPeriodId") for m in sch})[:30]}
            write_json(probe_path, res)
        step("probe", probe)

    # player pool + valuations
    pool = step("player_pool", lambda: client.player_pool(sp))
    if pool:
        write_json(out / "debug" / "sample_player.json", pool[:2])
        prow = N.players(pool, client.season)
        write_csv(out / "players.csv", prow)
        status["counts_players"] = len(prow)
        vals = step("valuations", lambda: V.build(prow, settings))
        if vals:
            rows, meta = vals
            left, this_mp = _games_left(sched_rows, mp_rows, sp, settings)
            for r in rows:
                r["team_games_left"] = left.get(r["pro_team"], "")
                r["team_games_this_matchup"] = this_mp.get(r["pro_team"], "")
            write_csv(out / "valuations.csv", rows)
            # birth dates / experience for keeper (next-season) value; cached
            if hasattr(client, "session"):
                rostered = {str(r["player_id"]) for r in (roster_rows or [])}
                wanted = [(r["player_id"], r["name"]) for r in rows[:300]
                          if r.get("player_id")]
                wanted += [(r["player_id"], r["name"]) for r in (roster_rows or [])
                           if str(r["player_id"]) not in {str(w[0]) for w in wanted}]
                res = step("bio", lambda: B.update(client.session, wanted, out / "player_bio.csv"))
                if res:
                    status["bio_fetched"], status["bio_failed"] = res[1], res[2]
            write_json(out / "valuation_meta.json", meta)
            status["warnings"] += meta.get("warnings", [])

    # current matchup scores, daily stat history (for grading), matchup engine
    cur_mp = settings["status"].get("current_matchup_period") or 1
    write_json(out / "current_scores.json", step("scores", lambda: N.current_scores(league, cur_mp)) or {})
    if pool:
        tot = step("history", lambda: N.season_totals(pool, client.season))
        if tot:
            day = datetime.now(timezone.utc).astimezone(N.CT).date().isoformat()
            write_csv(out / "history" / "totals" / f"{day}.csv", tot)

    if roster_rows and status.get("my_team_id") is not None:
        day = datetime.now(timezone.utc).astimezone(N.CT).date().isoformat()
        mine_rows = [r for r in roster_rows if str(r["team_id"]) == str(status["my_team_id"])]
        write_csv(out / "history" / "lineups" / f"{day}.csv", mine_rows)

    inj = step("injuries", client.injuries)
    if inj:
        write_csv(out / "injuries.csv", N.injuries(inj))
    nw = step("news", client.news)
    if nw:
        write_csv(out / "news.csv", N.news(nw))

    # small raw samples so parsing can be checked without the full payloads
    raw_settings = (league.get("settings") or {})
    write_json(out / "debug" / "settings_raw.json", raw_settings)
    write_json(out / "debug" / "league_keys.json", {
        "top_level_keys": sorted(league.keys()),
        "team_keys": sorted((league.get("teams") or [{}])[0].keys()),
        "status": league.get("status"),
    })

    status["ok"] = not status["errors"]
    write_json(out / "status.json", status)
    bd = step("matchup", lambda: MU.build(out))
    if bd:
        write_json(out / "brief_data.json", bd)
    status["ok"] = not status["errors"]
    write_json(out / "status.json", status)
    return status


def _games_left(sched_rows, mp_rows, sp, settings):
    final_sp = settings["status"].get("final_scoring_period") or 10 ** 6
    sp = max(int(sp or 0), 1)
    sp_to_mp = {r["scoring_period"]: r["matchup_period"] for r in mp_rows}
    cur_mp = sp_to_mp.get(sp)
    left, this_mp = {}, {}
    for r in sched_rows:
        if sp <= r["scoring_period"] <= final_sp:
            left[r["team"]] = left.get(r["team"], 0) + 1
            if cur_mp is not None and sp_to_mp.get(r["scoring_period"]) == cur_mp:
                this_mp[r["team"]] = this_mp.get(r["team"], 0) + 1
    return left, this_mp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="snapshots")
    a = ap.parse_args()
    league_id = os.environ.get("ESPN_LEAGUE_ID")
    s2, swid = os.environ.get("ESPN_S2"), os.environ.get("ESPN_SWID")
    season = int(os.environ.get("SEASON", "2027"))
    if not league_id:
        print("ESPN_LEAGUE_ID is not set", file=sys.stderr)
        sys.exit(2)
    client = EspnClient(league_id, season, s2, swid)
    st = run(a.out, client, swid)
    print(json.dumps({k: st[k] for k in ("ok", "steps", "warnings", "errors") if k in st}, indent=2))
    sys.exit(0 if st["ok"] else 1)


if __name__ == "__main__":
    main()
