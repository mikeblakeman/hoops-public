"""Turn raw ESPN JSON into small, tidy tables (lists of dicts)."""
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .constants import (DEFAULT_POS_MAP, EXPORT_STATS, PRO_TEAM_MAP, RATIO_STATS,
                        SLOT_MAP, SPLIT_PREFIX, STATS_MAP)

CT = ZoneInfo("America/Chicago")
NON_POSITION_SLOTS = {"BE", "IR", "UT", "", "Rookie"}


def ms_to_ct(ms):
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(CT)


def _iso(dt):
    return dt.isoformat(timespec="minutes") if dt else ""


# ---------------------------------------------------------------- settings
def settings_summary(league):
    s = league.get("settings", {}) or {}
    status = league.get("status", {}) or {}
    scoring = s.get("scoringSettings", {}) or {}
    roster = s.get("rosterSettings", {}) or {}
    acq = s.get("acquisitionSettings", {}) or {}
    trade = s.get("tradeSettings", {}) or {}
    draft = s.get("draftSettings", {}) or {}
    sched = s.get("scheduleSettings", {}) or {}

    items = []
    for it in scoring.get("scoringItems", []) or []:
        sid = it.get("statId")
        items.append({
            "stat_id": sid,
            "stat": STATS_MAP.get(sid, str(sid)),
            "points": it.get("points"),
            "reverse": bool(it.get("isReverseItem", False)),
        })

    slots = {}
    for k, v in (roster.get("lineupSlotCounts", {}) or {}).items():
        if v:
            slots[SLOT_MAP.get(int(k), k)] = v

    return {
        "league_name": s.get("name"),
        "season": league.get("seasonId"),
        "team_count": s.get("size"),
        "scoring_type": scoring.get("scoringType"),
        "scoring_items": items,
        "matchup_tie_rule": scoring.get("matchupTieRule"),
        "lineup_slots": slots,
        "roster_size": sum(slots.values()),
        "lineup_lock": roster.get("lineupLocktimeType"),
        "roster_lock": roster.get("rosterLocktimeType"),
        "move_limit": roster.get("moveLimit"),
        "position_limits": {SLOT_MAP.get(int(k), k): v
                            for k, v in (roster.get("positionLimits", {}) or {}).items() if v},
        "acquisition": {
            "season_limit": acq.get("acquisitionLimit"),
            "matchup_limit": acq.get("matchupAcquisitionLimit"),
            "limit_per_scoring_period": acq.get("matchupLimitPerScoringPeriod"),
            "type": acq.get("acquisitionType"),
            "waiver_hours": acq.get("waiverHours"),
            "waiver_process_days": acq.get("waiverProcessDays"),
            "waiver_process_hour": acq.get("waiverProcessHour"),
            "uses_budget_faab": acq.get("isUsingAcquisitionBudget"),
            "budget": acq.get("acquisitionBudget"),
        },
        "trade": {
            "deadline_ct": _iso(ms_to_ct(trade.get("deadlineDate"))),
            "veto_votes_required": trade.get("vetoVotesRequired"),
            "review_hours": trade.get("revisionHours"),
            "allow_out_of_position": trade.get("allowOutOfPosition"),
        },
        "draft": {
            "date_ct": _iso(ms_to_ct(draft.get("date"))),
            "type": draft.get("type"),
            "keeper_count": draft.get("keeperCount"),
            "keeper_order_type": draft.get("keeperOrderType"),
            "pick_order_team_ids": draft.get("pickOrder"),
            "seconds_per_pick": draft.get("timePerSelection"),
            "order_type": draft.get("orderType"),
        },
        "schedule": {
            "regular_season_matchups": sched.get("matchupPeriodCount"),
            "playoff_teams": sched.get("playoffTeamCount"),
            "playoff_matchup_length": sched.get("playoffMatchupPeriodLength"),
            "playoff_seeding_rule": sched.get("playoffSeedingRule"),
        },
        "status": {
            "current_matchup_period": status.get("currentMatchupPeriod"),
            "latest_scoring_period": status.get("latestScoringPeriod"),
            "first_scoring_period": status.get("firstScoringPeriod"),
            "final_scoring_period": status.get("finalScoringPeriod"),
            "is_active": status.get("isActive"),
            "previous_seasons": status.get("previousSeasons"),
        },
    }


def matchup_periods(league):
    mp = ((league.get("settings", {}) or {}).get("scheduleSettings", {}) or {}).get(
        "matchupPeriods", {}) or {}
    rows = []
    for period, sps in sorted(mp.items(), key=lambda kv: int(kv[0])):
        for sp in sps:
            rows.append({"matchup_period": int(period), "scoring_period": int(sp)})
    return rows


# ---------------------------------------------------------------- teams
def _team_name(t):
    if t.get("name"):
        return t["name"]
    return f"{t.get('location', '')} {t.get('nickname', '')}".strip()


def teams(league, my_swid=None):
    members = {m.get("id"): m for m in league.get("members", []) or []}
    cur = (league.get("status", {}) or {}).get("currentMatchupPeriod")
    rows = []
    for t in league.get("teams", []) or []:
        owners = t.get("owners", []) or []
        owner_names = []
        for o in owners:
            m = members.get(o, {})
            nm = m.get("displayName") or " ".join(
                x for x in [m.get("firstName"), m.get("lastName")] if x)
            owner_names.append(nm or o)
        rec = ((t.get("record", {}) or {}).get("overall", {}) or {})
        tc = t.get("transactionCounter", {}) or {}
        mat = tc.get("matchupAcquisitionTotals", {}) or {}
        rows.append({
            "team_id": t.get("id"),
            "abbrev": t.get("abbrev"),
            "name": _team_name(t),
            "owners": "; ".join(owner_names),
            "is_me": bool(my_swid and any(o.strip("{}").lower() == my_swid.strip("{}").lower()
                                          for o in owners)),
            "wins": rec.get("wins"), "losses": rec.get("losses"), "ties": rec.get("ties"),
            "points_for": rec.get("pointsFor"), "points_against": rec.get("pointsAgainst"),
            "playoff_seed": t.get("playoffSeed"),
            "waiver_rank": t.get("waiverRank"),
            "acquisitions": tc.get("acquisitions"), "drops": tc.get("drops"),
            "trades": tc.get("trades"),
            "acq_this_matchup": mat.get(str(cur), mat.get(cur, 0)) if cur else None,
            "draft_projected_rank": t.get("draftDayProjectedRank"),
            "current_projected_rank": t.get("currentProjectedRank"),
        })
    return rows


# ---------------------------------------------------------------- players
def _eligible(player):
    return "/".join(SLOT_MAP.get(s, str(s)) for s in player.get("eligibleSlots", []) or []
                    if SLOT_MAP.get(s, "") not in NON_POSITION_SLOTS
                    and SLOT_MAP.get(s, "") in {"PG", "SG", "SF", "PF", "C"})


def _split_key(split, season):
    sid = str(split.get("id", ""))
    if len(sid) < 6:
        return None
    prefix, yr = sid[:2], sid[2:]
    if yr == str(season) and prefix in SPLIT_PREFIX:
        return SPLIT_PREFIX[prefix]
    if yr == str(season - 1) and prefix == "00":
        return "prev"
    return None


def per_game_line(split):
    """Return per-game averages keyed by int stat id (plus GP/MIN totals)."""
    stats = {int(k): v for k, v in (split.get("stats") or {}).items() if str(k).isdigit()}
    avg = {int(k): v for k, v in (split.get("averageStats") or {}).items() if str(k).isdigit()}
    gp = stats.get(42) or 0
    line = {}
    if avg:
        line = dict(avg)
    elif gp:
        line = {k: (v / gp) for k, v in stats.items()}
    # Keep a split usable when ESPN sends real averages without a GP total;
    # drop splits that are all zeros (e.g. a season the player missed)
    has_avg = any(v for k, v in avg.items() if k != 42)
    line[42] = gp if gp else (1 if has_avg else 0)
    # Recompute ratios from makes/attempts so they are never stale
    for rid, (num, den) in RATIO_STATS.items():
        if line.get(den):
            line[rid] = line.get(num, 0) / line[den] if line[den] else None
    return line, split.get("appliedAverage"), split.get("appliedTotal")


def players(pool, season):
    rows = []
    for e in pool:
        p = e.get("player", {}) or {}
        own = p.get("ownership", {}) or {}
        ranks = (p.get("draftRanksByRankType", {}) or {}).get("STANDARD", {}) or {}
        row = {
            "player_id": p.get("id") or e.get("id"),
            "name": p.get("fullName"),
            "pro_team": PRO_TEAM_MAP.get(p.get("proTeamId"), p.get("proTeamId")),
            "pos": DEFAULT_POS_MAP.get(p.get("defaultPositionId"), ""),
            "eligible": _eligible(p),
            "status": e.get("status"),
            "on_team_id": e.get("onTeamId"),
            "injury_status": p.get("injuryStatus") or ("INJURED" if p.get("injured") else "ACTIVE"),
            "pct_owned": round(own.get("percentOwned", 0) or 0, 2),
            "pct_change": round(own.get("percentChange", 0) or 0, 2),
            "pct_started": round(own.get("percentStarted", 0) or 0, 2),
            "adp": round(own.get("averageDraftPosition", 0) or 0, 1),
            "espn_rank": ranks.get("rank"),
            "auction_value": ranks.get("auctionValue"),
            "keeper_value": e.get("keeperValue"),
            "lineup_locked": e.get("lineupLocked"),
        }
        for split in p.get("stats", []) or []:
            key = _split_key(split, season)
            if not key:
                continue
            line, fp_avg, fp_tot = per_game_line(split)
            row[f"{key}_gp"] = line.get(42, 0)
            row[f"{key}_fpts_avg"] = round(fp_avg, 2) if fp_avg is not None else ""
            for sid in EXPORT_STATS:
                if sid == 42:
                    continue
                v = line.get(sid)
                row[f"{key}_{STATS_MAP[sid]}"] = round(v, 3) if isinstance(v, (int, float)) else ""
            for rid in (19, 20):
                v = line.get(rid)
                row[f"{key}_{STATS_MAP[rid]}"] = round(v, 4) if isinstance(v, (int, float)) else ""
        rows.append(row)
    return rows


def rosters(league):
    rows = []
    for t in league.get("teams", []) or []:
        for en in ((t.get("roster", {}) or {}).get("entries", []) or []):
            pe = en.get("playerPoolEntry", {}) or {}
            p = pe.get("player", {}) or {}
            rows.append({
                "team_id": t.get("id"),
                "player_id": en.get("playerId") or p.get("id"),
                "name": p.get("fullName"),
                "slot": SLOT_MAP.get(en.get("lineupSlotId"), en.get("lineupSlotId")),
                "pos": DEFAULT_POS_MAP.get(p.get("defaultPositionId"), ""),
                "eligible": _eligible(p),
                "pro_team": PRO_TEAM_MAP.get(p.get("proTeamId"), p.get("proTeamId")),
                "injury_status": en.get("injuryStatus") or p.get("injuryStatus"),
                "acquisition_type": en.get("acquisitionType"),
                "acquired_ct": _iso(ms_to_ct(en.get("acquisitionDate"))),
                "keeper_value": pe.get("keeperValue"),
                "keeper_value_future": pe.get("keeperValueFuture"),
                "lineup_locked": pe.get("lineupLocked"),
            })
    return rows


# ---------------------------------------------------------------- schedule
def pro_schedule(sched_json):
    s = sched_json.get("settings", {}) or {}
    rows = []
    for team in s.get("proTeams", []) or []:
        tid = team.get("id")
        if not tid:
            continue
        for sp, games in (team.get("proGamesByScoringPeriod", {}) or {}).items():
            for g in games or []:
                home = g.get("homeProTeamId")
                away = g.get("awayProTeamId")
                opp = away if home == tid else home
                tip = ms_to_ct(g.get("date"))
                rows.append({
                    "scoring_period": int(sp),
                    "date_ct": tip.date().isoformat() if tip else "",
                    "tip_ct": tip.strftime("%H:%M") if tip else "",
                    "team": PRO_TEAM_MAP.get(tid, tid),
                    "opp": PRO_TEAM_MAP.get(opp, opp),
                    "home_away": "home" if home == tid else "away",
                    "game_id": g.get("id"),
                    "tip_tbd": bool(g.get("startTimeTBD", False)),
                })
    rows.sort(key=lambda r: (r["scoring_period"], r["tip_ct"], r["team"]))
    return rows


def scoring_period_dates(schedule_rows):
    """Map scoring period -> date. Fills no-game days by linear offset."""
    known = {}
    for r in schedule_rows:
        if r["date_ct"]:
            known.setdefault(r["scoring_period"], r["date_ct"])
    if not known:
        return {}, []
    base_sp = min(known)
    base = datetime.fromisoformat(known[base_sp]).date()
    out, warnings = {}, []
    for sp in range(1, max(known) + 1):
        d = (base + timedelta(days=sp - base_sp)).isoformat()
        if sp in known and known[sp] != d:
            warnings.append(f"scoring period {sp} date {known[sp]} != linear {d}")
            d = known[sp]
        out[sp] = d
    return out, warnings


def schedule_grid(schedule_rows, mp_rows, sp_dates):
    """Games per NBA team per matchup period, plus period date ranges."""
    sp_to_mp = {r["scoring_period"]: r["matchup_period"] for r in mp_rows}
    periods = {}
    for sp, mp in sp_to_mp.items():
        d = sp_dates.get(sp)
        if d:
            lo, hi = periods.get(mp, (d, d))
            periods[mp] = (min(lo, d), max(hi, d))
    counts = {}
    for r in schedule_rows:
        mp = sp_to_mp.get(r["scoring_period"])
        if mp is None:
            continue
        counts[(mp, r["team"])] = counts.get((mp, r["team"]), 0) + 1
    teams_ = sorted({r["team"] for r in schedule_rows})
    rows = []
    for mp in sorted(periods):
        for t in teams_:
            rows.append({"matchup_period": mp, "start": periods[mp][0], "end": periods[mp][1],
                         "team": t, "games": counts.get((mp, t), 0)})
    return rows


def daily_slate(schedule_rows):
    by_date = {}
    for r in schedule_rows:
        by_date.setdefault((r["scoring_period"], r["date_ct"]), set()).add(r["game_id"])
    rows = [{"scoring_period": sp, "date_ct": d, "games": len(g)}
            for (sp, d), g in sorted(by_date.items())]
    return rows


# ---------------------------------------------------------------- matchups / draft
def matchups(league):
    rows = []
    for m in league.get("schedule", []) or []:
        def side(x):
            x = x or {}
            cs = x.get("cumulativeScore", {}) or {}
            return x.get("teamId"), x.get("totalPoints"), cs.get("wins"), cs.get("losses"), cs.get("ties")
        h, a = side(m.get("home")), side(m.get("away"))
        rows.append({
            "matchup_period": m.get("matchupPeriodId"), "matchup_id": m.get("id"),
            "home_team_id": h[0], "home_points": h[1], "home_cat_w": h[2], "home_cat_l": h[3], "home_cat_t": h[4],
            "away_team_id": a[0], "away_points": a[1], "away_cat_w": a[2], "away_cat_l": a[3], "away_cat_t": a[4],
            "winner": m.get("winner"), "playoff_tier": m.get("playoffTierType"),
        })
    return rows


def draft_picks(league):
    dd = league.get("draftDetail", {}) or {}
    rows = [{
        "overall": p.get("overallPickNumber"), "round": p.get("roundId"),
        "round_pick": p.get("roundPickNumber"), "team_id": p.get("teamId"),
        "player_id": p.get("playerId"), "keeper": p.get("keeper"),
        "reserved_for_keeper": p.get("reservedForKeeper"),
    } for p in dd.get("picks", []) or []]
    meta = {"drafted": dd.get("drafted"), "in_progress": dd.get("inProgress")}
    return rows, meta


# ---------------------------------------------------------------- injuries / news
_ID_RE = re.compile(r"/id/(\d+)")


def _athlete_id(a):
    if a.get("id"):
        return a["id"]
    for link in a.get("links", []) or []:
        m = _ID_RE.search(link.get("href", ""))
        if m:
            return m.group(1)
    return ""


def injuries(inj_json):
    rows = []
    for team in inj_json.get("injuries", []) or []:
        for i in team.get("injuries", []) or []:
            a = i.get("athlete", {}) or {}
            det = i.get("details", {}) or {}
            rows.append({
                "team": team.get("displayName"),
                "player_id": _athlete_id(a),
                "name": a.get("displayName"),
                "status": i.get("status"),
                "updated": i.get("date"),
                "return_date": det.get("returnDate", ""),
                "detail": det.get("type", ""),
                "comment": (i.get("shortComment") or "").replace("\n", " "),
            })
    return rows


def news(news_json):
    rows = []
    for a in news_json.get("articles", []) or []:
        ath = [str(c.get("athleteId")) for c in a.get("categories", []) or []
               if c.get("type") == "athlete" and c.get("athleteId")]
        rows.append({
            "published": a.get("published"),
            "headline": a.get("headline"),
            "description": (a.get("description") or "").replace("\n", " "),
            "athlete_ids": ";".join(ath),
        })
    return rows


def current_scores(league, matchup_period):
    """Per-team category totals so far in a matchup period: {team_id: {stat_id: score}}."""
    out = {}
    for m in league.get("schedule", []) or []:
        if m.get("matchupPeriodId") != matchup_period:
            continue
        for side in ("home", "away"):
            x = m.get(side) or {}
            sbs = ((x.get("cumulativeScore") or {}).get("scoreByStat") or {})
            out[str(x.get("teamId"))] = {str(k): (v or {}).get("score") for k, v in sbs.items()}
    return out


def season_totals(pool, season):
    """Cumulative season counting stats per player (for daily diffs / grading)."""
    keep = [0, 1, 2, 3, 6, 11, 13, 14, 15, 16, 17, 18, 37, 40, 42]
    rows = []
    for e in pool:
        p = e.get("player", {}) or {}
        for split in p.get("stats", []) or []:
            if str(split.get("id")) == f"00{season}":
                st = {int(k): v for k, v in (split.get("stats") or {}).items() if str(k).isdigit()}
                if st.get(42):
                    row = {"player_id": p.get("id"), "name": p.get("fullName")}
                    row.update({STATS_MAP[k]: st.get(k, 0) for k in keep})
                    rows.append(row)
    return rows
