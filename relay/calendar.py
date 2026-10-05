"""Fantasy matchup calendar (which days belong to which matchup period).

ESPN basketball's settings list matchup periods without dates, so dates are
built here, in priority order:
  1. relay/calendar_override.json  {"17": ["2027-02-15", "2027-02-28"], ...}
  2. ESPN actuals: schedule[].home.pointsByScoringPeriod (only after games start)
  3. Inference: Monday-Sunday weeks from opening day. With one more week than
     matchup periods, ESPN merges either the All-Star week with the next week
     or week 1 with week 2. Both are built; the one matching observed data
     (today's matchup period, per-day scoring) wins, defaulting to All-Star.
     Playoff weeks are the same under both.
"""
import json
from datetime import date, timedelta
from pathlib import Path

OVERRIDE = Path(__file__).with_name("calendar_override.json")


def _espn_actuals(league):
    sp_to_mp = {}
    for m in league.get("schedule", []) or []:
        mp = m.get("matchupPeriodId")
        for side in ("home", "away"):
            for sp in ((m.get(side) or {}).get("pointsByScoringPeriod") or {}).keys():
                sp_to_mp[int(sp)] = mp
    return sp_to_mp


def build(league, sp_dates, slate_rows, n_periods, regular_count):
    games = {int(r["scoring_period"]): int(r["games"]) for r in slate_rows}
    sps = sorted(sp_dates)
    if not sps:
        return [], []

    # 3. inference
    d1 = date.fromisoformat(sp_dates[sps[0]])
    monday0 = d1 - timedelta(days=d1.weekday())
    weeks = {}
    for sp in sps:
        wi = (date.fromisoformat(sp_dates[sp]) - monday0).days // 7
        weeks.setdefault(wi, []).append(sp)
    base = [weeks[k] for k in sorted(weeks)]
    playoff_n = max(n_periods - regular_count, 0)

    def allstar_merge(blocks):
        blocks = [list(b) for b in blocks]
        while len(blocks) > n_periods and len(blocks) > 2:
            last_regular = len(blocks) - playoff_n - 1
            cands = list(range(1, max(last_regular, 1)))  # never week 1 or playoffs
            if not cands:
                break

            def zero_days(i):
                return sum(1 for sp in blocks[i] if games.get(sp, 0) == 0)

            def month(i, m):
                return any(sp_dates[sp][5:7] == m for sp in blocks[i])
            # All-Star break: the February week with the most zero-game days
            # (ties -> the earlier week, i.e. the week containing the game).
            feb = [i for i in cands if month(i, "02")]
            if feb:
                idx = max(feb, key=lambda i: (zero_days(i), -i))
            else:
                # December gaps are NBA Cup games not yet scheduled; ignore them
                pool = [i for i in cands if not month(i, "12")] or cands
                idx = min(pool, key=lambda i: sum(games.get(sp, 0) for sp in blocks[i]))
            blocks[idx] = blocks[idx] + blocks.pop(idx + 1)
        return blocks

    hypotheses = {"allstar": allstar_merge(base)}
    if len(base) - n_periods == 1:
        # ESPN sometimes stretches matchup 1 through the second Sunday instead
        hypotheses["week1"] = [base[0] + base[1]] + [list(b) for b in base[2:]]

    # Evidence: ESPN per-day scoring (after games start) and today's matchup period
    observed = dict(_espn_actuals(league))
    st = league.get("status") or {}
    if (st.get("latestScoringPeriod") or 0) >= 1 and st.get("currentMatchupPeriod"):
        observed[int(st["latestScoringPeriod"])] = st["currentMatchupPeriod"]

    def fit(blocks):
        m = {sp: i + 1 for i, b in enumerate(blocks) for sp in b}
        return sum(1 for sp, mp in observed.items() if m.get(sp) == mp)
    chosen = max(hypotheses, key=lambda h: (fit(hypotheses[h]), h == "allstar"))
    blocks = hypotheses[chosen]
    sp_to_mp = {sp: i + 1 for i, b in enumerate(blocks) for sp in b}
    label = f"inferred:{chosen}" + ("" if observed else ":unconfirmed")
    source = {sp: label for sp in sp_to_mp}

    # 2. ESPN actuals win where they exist
    for sp, mp in _espn_actuals(league).items():
        sp_to_mp[sp] = mp
        source[sp] = "espn"

    # 1. manual override wins over everything
    if OVERRIDE.exists():
        for mp, (start, end) in json.loads(OVERRIDE.read_text()).items():
            for sp, d in sp_dates.items():
                if start <= d <= end:
                    sp_to_mp[sp] = int(mp)
                    source[sp] = "override"

    rows = []
    for mp in sorted(set(sp_to_mp.values())):
        members = sorted(sp for sp, m in sp_to_mp.items() if m == mp)
        srcs = {source[sp] for sp in members}
        rows.append({
            "matchup_period": mp,
            "start": sp_dates[members[0]],
            "end": sp_dates[members[-1]],
            "first_sp": members[0],
            "last_sp": members[-1],
            "days": len(members),
            "nba_games": sum(games.get(sp, 0) for sp in members),
            "playoffs": mp > regular_count,
            "source": "/".join(sorted(srcs)),
        })
    sp_rows = [{"matchup_period": sp_to_mp[sp], "scoring_period": sp} for sp in sorted(sp_to_mp)]
    return rows, sp_rows
