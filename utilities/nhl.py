"""
NHL scoreboard helper — fetches today's game state for a configured team.

Uses the official NHL Stats API (free, no auth required):
  https://api-web.nhle.com/v1/scoreboard/now

Only returns a game if it is scheduled for TODAY in the local calendar
(prevents showing tomorrow's game after midnight in the API but before
the local date rolls over).
"""

import datetime
import sys
import requests
from zoneinfo import ZoneInfo

# Default team ID — Vegas Golden Knights (used as fallback when not configured)
VGK_TEAM_ID       = 54
_SCOREBOARD_URL   = "https://api-web.nhle.com/v1/scoreboard/now"

# Single module-level Session shared across all callers.  Each poll runs on a
# fresh daemon thread (sportscore._poll_slot_async), so a thread-local Session
# would be re-created every poll and never actually reuse a TCP connection —
# defeating keep-alive.  A requests.Session is safe for concurrent simple GETs,
# so one shared Session lets the underlying connection pool persist across polls
# (cuts per-poll latency from ~150 ms to ~20 ms via TCP/TLS reuse).
_session = requests.Session()


def _get_session() -> requests.Session:
    """Return the shared module-level requests.Session."""
    return _session

_PERIOD_NAMES = {1: "1st", 2: "2nd", 3: "3rd"}


def _period_label(number, period_type, in_intermission):
    """Return a compact period label like '2nd', 'OT', '2OT', 'SO', '2nd INT'.

    Playoff overtime is sudden-death and can run multiple full periods.  NHL
    regulation is 3 periods, so period 4 = 1st OT ('OT'), 5 = 2nd OT ('2OT'),
    6 = '3OT', … — important for a Stanley Cup Final that can go to double/triple OT.
    """
    if period_type == "OT":
        ot   = number - 3            # period 4 -> 1st OT, 5 -> 2nd OT, …
        base = "OT" if ot <= 1 else f"{ot}OT"
    elif period_type == "SO":
        base = "SO"
    else:
        base = _PERIOD_NAMES.get(number, f"P{number}")
    return f"{base} INT" if in_intermission else base


def fetch_game(team_id: int, tz: ZoneInfo = None):
    """
    Return a dict describing today's game for the given NHL team_id, or None.

    Dict keys
    ---------
    state           : str  — "FUT" | "PRE" | "LIVE" | "CRIT" | "FINAL" | "OFF"
    team_score      : int  — configured team's score
    opp_score       : int  — opponent's score
    opp_abbr        : str  — e.g. "EDM"
    team_home       : bool — True if the configured team is the home team
    period          : int  — current/last period number (0 if not started)
    period_type     : str  — "REG" | "OT" | "SO"
    period_label    : str  — human label, e.g. "2nd", "OT", "2nd INT"
    time_remaining  : str  — clock string, e.g. "14:32" (empty if not live)
    in_intermission : bool
    start_time_utc  : str  — ISO-8601 UTC start time
    game_id         : int
    """
    try:
        r = _get_session().get(_SCOREBOARD_URL, timeout=6)
        if r.status_code != 200:
            return None

        # Determine today's local date so we don't accidentally show
        # tomorrow's game when the API returns the full coming week.
        today = datetime.date.today() if tz is None else datetime.datetime.now(tz).date()
        today_str = today.isoformat()

        for day_block in r.json().get("gamesByDate", []):
            if day_block.get("date") != today_str:
                continue
            for game in day_block.get("games", []):
                home = game.get("homeTeam", {})
                away = game.get("awayTeam",  {})
                if home.get("id") != team_id and away.get("id") != team_id:
                    continue

                team_home = home.get("id") == team_id
                team      = home if team_home else away
                opp       = away if team_home else home
                pd        = game.get("periodDescriptor", {})
                clock     = game.get("clock", {})
                in_int    = clock.get("inIntermission", False)
                ptype     = pd.get("periodType", "REG")
                pnum      = pd.get("number", 0)

                return {
                    "state":           game.get("gameState", "FUT"),
                    "team_score":      team.get("score", 0),
                    "opp_score":       opp.get("score", 0),
                    "opp_abbr":        opp.get("abbrev", "???"),
                    "team_home":       team_home,
                    "period":          pnum,
                    "period_type":     ptype,
                    "period_label":    _period_label(pnum, ptype, in_int),
                    "time_remaining":  clock.get("timeRemaining", ""),
                    "in_intermission": in_int,
                    "start_time_utc":  game.get("startTimeUTC", ""),
                    "game_id":         game.get("id"),
                }
        return None

    except Exception as e:
        print(f"[nhl] fetch_game error: {e}", file=sys.stderr, flush=True)
        return None


def fetch_vgk_game(tz: ZoneInfo = None):
    """Deprecated — use fetch_game(team_id, tz) directly."""
    return fetch_game(VGK_TEAM_ID, tz)


def game_start_local(start_time_utc: str, tz: ZoneInfo, time_format: str = "12h") -> str:
    """
    Convert a UTC ISO start time to a local time string.

    time_format : "12h" → "7:00 PM"  (default, backward-compatible)
                  "24h" → "19:00"
    """
    try:
        utc_dt   = datetime.datetime.fromisoformat(start_time_utc.replace("Z", "+00:00"))
        local_dt = utc_dt.astimezone(tz)
        h, m     = local_dt.hour, local_dt.minute
        if time_format == "24h":
            return f"{h:02d}:{m:02d}"
        ampm = "AM" if h < 12 else "PM"
        return f"{h % 12 or 12}:{m:02d} {ampm}"
    except Exception:
        return ""
