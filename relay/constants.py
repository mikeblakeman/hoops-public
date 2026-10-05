"""ESPN fantasy basketball id maps (mirrors the community espn-api package)."""

SLOT_MAP = {
    0: "PG", 1: "SG", 2: "SF", 3: "PF", 4: "C", 5: "G", 6: "F", 7: "SG/SF",
    8: "G/F", 9: "PF/C", 10: "F/C", 11: "UT", 12: "BE", 13: "IR", 14: "", 15: "Rookie",
}

# defaultPositionId is 1-based: 1=PG ... 5=C
DEFAULT_POS_MAP = {1: "PG", 2: "SG", 3: "SF", 4: "PF", 5: "C"}

PRO_TEAM_MAP = {
    0: "FA", 1: "ATL", 2: "BOS", 3: "NOP", 4: "CHI", 5: "CLE", 6: "DAL", 7: "DEN",
    8: "DET", 9: "GSW", 10: "HOU", 11: "IND", 12: "LAC", 13: "LAL", 14: "MIA",
    15: "MIL", 16: "MIN", 17: "BKN", 18: "NYK", 19: "ORL", 20: "PHL", 21: "PHO",
    22: "POR", 23: "SAC", 24: "SAS", 25: "OKC", 26: "UTA", 27: "WAS", 28: "TOR",
    29: "MEM", 30: "CHA",
}

STATS_MAP = {
    0: "PTS", 1: "BLK", 2: "STL", 3: "AST", 4: "OREB", 5: "DREB", 6: "REB",
    7: "EJ", 8: "FF", 9: "PF", 10: "TF", 11: "TO", 12: "DQ", 13: "FGM", 14: "FGA",
    15: "FTM", 16: "FTA", 17: "3PM", 18: "3PA", 19: "FG%", 20: "FT%", 21: "3PT%",
    22: "AFG%", 23: "FGMI", 24: "FTMI", 25: "3PMI", 26: "APG", 27: "BPG", 28: "MPG",
    29: "PPG", 30: "RPG", 31: "SPG", 32: "TOPG", 33: "3PG", 34: "PPM", 35: "A/TO",
    36: "STR", 37: "DD", 38: "TD", 39: "QD", 40: "MIN", 41: "GS", 42: "GP",
    43: "TW", 44: "FTR",
}

# Ratio categories: stat id -> (numerator stat id, denominator stat id)
RATIO_STATS = {19: (13, 14), 20: (15, 16), 21: (17, 18), 35: (3, 11)}

# Stat split id prefixes: first two chars of split "id" (e.g. "002027")
SPLIT_PREFIX = {
    "00": "season",     # actual, season to date
    "10": "proj",       # ESPN projection, full season
    "01": "l7",
    "02": "l15",
    "03": "l30",
}

# Stats exported per split in players.csv (per-game averages)
EXPORT_STATS = [0, 6, 3, 2, 1, 17, 18, 11, 13, 14, 15, 16, 4, 5, 37, 38, 40, 42]
