"""Thin, read-only client for ESPN's (unofficial) fantasy basketball API.

Private leagues need the espn_s2 and SWID cookies from a logged-in browser.
All calls are GETs. Nothing here can change a roster.
"""
import json
import time

import requests

FANTASY_BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba"
SITE_BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"


class EspnAuthError(RuntimeError):
    pass


class EspnClient:
    def __init__(self, league_id, season, espn_s2=None, swid=None, timeout=30):
        self.league_id = str(league_id)
        self.season = int(season)
        self.timeout = timeout
        self.cookies = {}
        if espn_s2 and swid:
            swid = swid.strip()
            if not swid.startswith("{"):
                swid = "{" + swid.strip("{}") + "}"
            self.cookies = {"espn_s2": espn_s2.strip(), "SWID": swid}
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (hoops-relay; read-only)",
            "Accept": "application/json",
        })

    # ---------- low level ----------
    def _get(self, url, params=None, headers=None, auth=True, retries=3):
        last = None
        for attempt in range(retries):
            r = self.session.get(
                url, params=params, headers=headers,
                cookies=self.cookies if auth else None, timeout=self.timeout,
            )
            if r.status_code in (401, 403):
                raise EspnAuthError(
                    f"ESPN returned {r.status_code} for {url}. The espn_s2/SWID "
                    "cookies are missing, wrong, or expired. Refresh the repo secrets."
                )
            if r.status_code == 200:
                return r.json()
            last = r
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt * 3)
                continue
            break
        raise RuntimeError(
            f"ESPN request failed ({last.status_code if last else 'n/a'}) for {url}: "
            f"{last.text[:300] if last is not None else ''}"
        )

    @property
    def league_url(self):
        return f"{FANTASY_BASE}/seasons/{self.season}/segments/0/leagues/{self.league_id}"

    # ---------- league ----------
    def league(self, views, scoring_period=None):
        params = [("view", v) for v in views]
        if scoring_period is not None:
            params.append(("scoringPeriodId", scoring_period))
        data = self._get(self.league_url, params=params)
        return data[0] if isinstance(data, list) else data

    def player_pool(self, scoring_period, statuses=("FREEAGENT", "WAIVERS", "ONTEAM"),
                    max_players=900, page=300):
        """League player pool with stat splits, ownership, ranks and injury status."""
        season = self.season
        split_ids = [f"00{season}", f"10{season}", f"01{season}", f"02{season}",
                     f"03{season}", f"00{season - 1}"]
        out, offset = [], 0
        while offset < max_players:
            flt = {"players": {
                "filterStatus": {"value": list(statuses)},
                "limit": page,
                "offset": offset,
                "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
                "sortDraftRanks": {"sortPriority": 100, "sortAsc": True, "value": "STANDARD"},
                "filterRanksForRankTypes": {"value": ["STANDARD"]},
                "filterStatsForTopScoringPeriodIds": {
                    "value": max(int(scoring_period or 1), 1),
                    "additionalValue": split_ids,
                },
            }}
            data = self._get(
                self.league_url,
                params=[("view", "kona_player_info"), ("scoringPeriodId", scoring_period or 1)],
                headers={"x-fantasy-filter": json.dumps(flt)},
            )
            batch = data.get("players", []) if isinstance(data, dict) else []
            out.extend(batch)
            if len(batch) < page:
                break
            offset += page
        return out

    # ---------- season-level ----------
    def pro_schedule(self):
        return self._get(f"{FANTASY_BASE}/seasons/{self.season}",
                         params={"view": "proTeamSchedules_wl"}, auth=False)

    def season_info(self):
        return self._get(f"{FANTASY_BASE}/seasons/{self.season}", auth=False)

    # ---------- public NBA news / injuries ----------
    def injuries(self):
        return self._get(f"{SITE_BASE}/injuries", auth=False)

    def news(self, limit=60):
        return self._get(f"{SITE_BASE}/news", params={"limit": limit}, auth=False)
