"""
MLB Stats API — official, free, no authentication required.

Docs / base URL:
  https://statsapi.mlb.com/api/v1/schedule

Returns a game dict in the same shape as utilities.nhl.fetch_game() so the
sport-score scene can treat all leagues uniformly.

Team IDs: look up at
  https://statsapi.mlb.com/api/v1/teams?sportId=1
"""

import datetime
import sys
import requests
from zoneinfo import ZoneInfo

_SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"

# Single module-level Session shared across all callers.  Each poll runs on a
# fresh daemon thread (sportscore._poll_slot_async), so a thread-local Session
# would be re-created every poll and never reuse a TCP connection — defeating
# keep-alive.  A requests.Session is safe for concurrent simple GETs, so one
# shared Session lets the connection pool persist across polls.
_session = requests.Session()


def _get_session() -> requests.Session:
    """Return the shared module-level requests.Session."""
    return _session


def fetch_mlb_game(team_id: int, tz: ZoneInfo = None) -> dict | None:
    """
    Fetch today's game for *team_id* from the official MLB Stats API.

    Returns the same dict shape as utilities.nhl.fetch_game(), or None when
    no game is scheduled today for that team.

    Parameters
    ----------
    team_id : int
        MLB Stats API team ID.  Look up at:
        https://statsapi.mlb.com/api/v1/teams?sportId=1
    tz : ZoneInfo, optional
        Local timezone used to determine "today's" date.
    """
    if not team_id:
        return None

    try:
        today = datetime.date.today() if tz is None else datetime.datetime.now(tz).date()
        params = {
            "sportId":  1,
            "date":     today.isoformat(),
            "teamId":   team_id,
            "hydrate":  "linescore,team",
        }
        r = _get_session().get(_SCHEDULE_URL, params=params, timeout=6)
        if r.status_code != 200:
            return None

        dates = r.json().get("dates", [])
        if not dates:
            return None

        # Note: MLB can schedule doubleheaders (two games same day).
        # We return the first game that matches the team — game 2 of a
        # doubleheader is ignored.  Acceptable trade-off for a single LED display.
        for game in dates[0].get("games", []):
            teams    = game.get("teams", {})
            home     = teams.get("home", {})
            away     = teams.get("away", {})
            home_id  = home.get("team", {}).get("id")
            away_id  = away.get("team", {}).get("id")

            # Compare as strings: config team_id is an int but the API may return
            # the id as an int or a string ("119"); ESPN already compares as str.
            if str(team_id) not in (str(home_id), str(away_id)):
                continue

            team_home = str(home_id) == str(team_id)
            team_data = home if team_home else away
            opp_data  = away if team_home else home

            # ── Game state ────────────────────────────────────────────────
            abstract = game.get("status", {}).get("abstractGameState", "Preview")
            if abstract == "Preview":
                state = "FUT"
            elif abstract == "Live":
                state = "LIVE"
            elif abstract == "Final":
                state = "FINAL"
            else:
                state = "FUT"

            # ── Inning / linescore ────────────────────────────────────────
            ls          = game.get("linescore", {})
            inning      = int(ls.get("currentInning") or 0)
            is_top      = ls.get("isTopInning", True)
            inning_half = ls.get("inningHalf", "")   # "Top", "Bottom", "End", "Middle"
            in_int      = inning_half in ("Middle", "End")

            # Period label: "TOP 7", "BOT 3", "F/10" for extra innings, "FINAL"
            if state == "FINAL":
                period_label = f"F/{inning}" if inning > 9 else "FINAL"
            elif state == "LIVE":
                half_abbr    = "TOP" if is_top else "BOT"
                period_label = f"{half_abbr} {inning}" if inning else ""
            else:
                period_label = ""

            return {
                "state":           state,
                "team_score":      int(team_data.get("score") or 0),
                "opp_score":       int(opp_data.get("score") or 0),
                "opp_abbr":        opp_data.get("team", {}).get("abbreviation", "???"),
                "team_home":       team_home,
                "period":          inning,
                "period_type":     "REG" if inning <= 9 else "OT",
                "period_label":    period_label,
                "time_remaining":  "",
                "in_intermission": in_int,
                "start_time_utc":  game.get("gameDate", ""),
                "game_id":         int(game.get("gamePk") or 0),
            }

        return None

    except Exception as e:
        print(f"[mlb] fetch_mlb_game error: {e}", file=sys.stderr, flush=True)
        return None
