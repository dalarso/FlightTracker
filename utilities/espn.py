"""
ESPN unofficial scoreboard API — covers NFL, NBA, MLS (and more).

Uses the public ESPN scoreboard endpoint (no auth required):
  https://site.api.espn.com/apis/site/v2/sports/{sport_path}/scoreboard

sport_path values used by this project:
  football/nfl   — NFL
  basketball/nba — NBA
  soccer/usa.1   — MLS

The ?dates=YYYYMMDD query parameter restricts results to a single calendar
day (local date), so we never accidentally show yesterday's or tomorrow's game.
"""

import datetime
import sys
import requests
from zoneinfo import ZoneInfo

_BASE_URL = "https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"

# Single module-level Session shared across all callers.  Each poll runs on a
# fresh daemon thread (sportscore._poll_slot_async), so a thread-local Session
# would be re-created every poll and never reuse a TCP connection — defeating
# keep-alive.  A requests.Session is safe for concurrent simple GETs, so one
# shared Session lets the connection pool persist across polls.
_session = requests.Session()


def _get_session() -> requests.Session:
    """Return the shared module-level requests.Session."""
    return _session


# ── ESPN status name → normalised game state ───────────────────────────────────
_STATE_MAP: dict[str, str] = {
    "STATUS_SCHEDULED":        "FUT",
    "STATUS_IN_PROGRESS":      "LIVE",
    "STATUS_HALFTIME":         "LIVE",   # in_intermission=True
    "STATUS_END_PERIOD":       "LIVE",   # in_intermission=True
    "STATUS_END_OF_EXTRATIME": "LIVE",   # in_intermission=True
    "STATUS_FINAL":            "FINAL",
    "STATUS_FULL_TIME":        "FINAL",
    "STATUS_FINAL_OVERTIME":   "FINAL",
    "STATUS_FINAL_PEN":        "FINAL",
    "STATUS_POSTPONED":        "OFF",
    "STATUS_CANCELED":         "OFF",
    "STATUS_SUSPENDED":        "OFF",
    "STATUS_RAIN_DELAY":       "PRE",   # game suspended — not actively in progress
}

_INTERMISSION_STATUSES: frozenset[str] = frozenset({
    "STATUS_HALFTIME",
    "STATUS_END_PERIOD",
    "STATUS_END_OF_EXTRATIME",
})


# ── Per-sport period label builders ───────────────────────────────────────────

def _label_nfl(period: int, status: str) -> str:
    if status == "STATUS_HALFTIME":
        return "HALF"
    base = {1: "Q1", 2: "Q2", 3: "Q3", 4: "Q4"}.get(period, "OT")
    return f"{base} END" if status == "STATUS_END_PERIOD" else base


def _label_nba(period: int, status: str) -> str:
    if status == "STATUS_HALFTIME":
        return "HALF"
    if period <= 4:
        # .get(), not a hard subscript: a scheduled-but-not-started game reports period 0
        # (which satisfies period <= 4); a bare [0] would KeyError, and the swallowing caller
        # would drop the game to None instead of showing the pre-game slot. The label is
        # unused for FUT/PRE anyway, so any sane default is fine.
        base = {1: "Q1", 2: "Q2", 3: "Q3", 4: "Q4"}.get(period, "Q1")
    elif period == 5:
        base = "OT"
    else:
        base = f"OT{period - 4}"
    return f"{base} END" if status == "STATUS_END_PERIOD" else base


def _label_mls(period: int, status: str) -> str:
    if status in _INTERMISSION_STATUSES:
        return "HLF"
    return {1: "1st", 2: "2nd"}.get(period, "ET")


_PERIOD_LABEL_FN: dict = {
    "football/nfl":    _label_nfl,
    "basketball/nba":  _label_nba,
    "basketball/wnba": _label_nba,   # WNBA — 4 quarters, identical period labels to the NBA
    "soccer/usa.1":    _label_mls,   # _label_mls is a generic soccer labeler (1st/2nd/HLF/ET)
    "soccer/fifa.world": _label_mls,  # FIFA World Cup — same period labels as any soccer match
}


# ── Public API ─────────────────────────────────────────────────────────────────

def fetch_espn_game(sport_path: str, team_id: int, tz: ZoneInfo = None) -> dict | None:
    """
    Fetch today's game for *team_id* from the ESPN scoreboard API.

    Returns the same dict shape as utilities.nhl.fetch_game(), or None when
    no game is scheduled today or the team is not found.

    Parameters
    ----------
    sport_path : str
        ESPN sport path — e.g. "football/nfl", "basketball/nba", "soccer/usa.1"
    team_id : int
        ESPN team ID.  Look up at:
        https://site.api.espn.com/apis/site/v2/sports/{sport_path}/teams
    tz : ZoneInfo, optional
        Local timezone used to derive "today's" date for the ?dates= param.
    """
    if not team_id:
        return None

    try:
        today  = datetime.date.today() if tz is None else datetime.datetime.now(tz).date()
        url    = _BASE_URL.format(path=sport_path)
        r      = _get_session().get(url, params={"dates": today.strftime("%Y%m%d")}, timeout=6)
        if r.status_code != 200:
            return None

        label_fn = _PERIOD_LABEL_FN.get(sport_path, lambda p, s: str(p))

        for event in r.json().get("events", []):
            comps = event.get("competitions", [])
            if not comps:
                continue
            comp        = comps[0]
            competitors = comp.get("competitors", [])
            if len(competitors) < 2:
                continue

            # Match competitor by team.id (string comparison to handle int/str)
            team_c = next(
                (c for c in competitors
                 if str(c.get("team", {}).get("id", "")) == str(team_id)),
                None,
            )
            if team_c is None:
                continue
            opp_c = next(c for c in competitors if c is not team_c)

            status    = event.get("status", {})
            stype     = status.get("type", {})
            sname     = stype.get("name", "STATUS_SCHEDULED")
            period    = int(status.get("period") or 0)
            state     = _STATE_MAP.get(sname, "FUT")
            in_int    = sname in _INTERMISSION_STATUSES
            team_home = team_c.get("homeAway") == "home"

            return {
                "state":           state,
                "team_score":      int(team_c.get("score") or 0),
                "opp_score":       int(opp_c.get("score") or 0),
                "opp_abbr":        opp_c.get("team", {}).get("abbreviation", "???"),
                "team_home":       team_home,
                "period":          period,
                "period_type":     "REG" if period <= 4 else "OT",
                "period_label":    label_fn(period, sname),
                "time_remaining":  status.get("displayClock", ""),
                "in_intermission": in_int,
                "start_time_utc":  event.get("date", ""),
                "game_id":         int(event.get("id") or 0),
            }

        return None

    except Exception as e:
        print(f"[espn:{sport_path}] fetch_espn_game error: {e}", file=sys.stderr, flush=True)
        return None
