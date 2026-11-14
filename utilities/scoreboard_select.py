"""Shared scoreboard game-selection contract.

Both the web GUI (web/scoreboard_data.py) and the LED display
(scenes/sportscore.py) must pick the SAME game when several configured sports
have one: walk SCOREBOARD_PRIORITY, take the first LIVE/CRIT game, else the
first FINAL/OFF game, else nothing.  That ordering used to be coded TWICE — kept
in sync only by tests/test_scoreboard_parity.py — and now lives here once.

The one legitimate difference between the two callers — the LED display drops a
FINAL once its post-game window has lapsed, while the web side leaves that to its
caller (server.py, via the persisted ended-at file) — is expressed through the
optional `final_eligible` predicate, NOT by forking the selection order.
"""

import datetime

LIVE_STATES = ("LIVE", "CRIT")
FINAL_STATES = ("FINAL", "OFF")

# Typical wall-clock length (scheduled start → final buzzer) per league, with ~30 min of
# headroom baked in for OT / stoppages so we err slightly LATE (never hide a score before its
# real post-game window ends).  Keyed by league because "period" means different things per
# sport: a completed BASKETBALL game ends at period 4, a BASEBALL game at inning 9 — so the old
# "period > 3 → 4 h" heuristic gave a ~2 h WNBA game a 4 h buffer and re-showed it for hours.
GAME_SECONDS = {
    "NHL":  int(3.0 * 3600),
    "MLB":  int(3.5 * 3600),
    "NFL":  int(3.5 * 3600),
    "NBA":  int(2.75 * 3600),
    "WNBA": int(2.5 * 3600),
    "MLS":  int(2.5 * 3600),
    "FIFA": int(2.75 * 3600),
}
DEFAULT_GAME_SECONDS = int(3.5 * 3600)   # err long for an unknown league


def estimate_ended_at(game, now_ts, league=None):
    """Best-effort end time for a FINAL/OFF game we did NOT watch finish — i.e. a service
    restart INTO an already-finished game (or, on the web side, a final first seen when the
    page is opened).  Estimated as the scheduled start plus that league's typical game length
    (GAME_SECONDS), clamped to now so it is never in the future.

    Without this a restart stamped `now`, giving a game that ended hours ago a fresh post-game
    window (a redeploy / reboot resurrected the last final for 30 min).  Falls back to now_ts
    when the start time is missing/unparseable.  Shared by the LED display and the web UI so
    both agree on when a final leaves the board."""
    try:
        start = (game or {}).get("start_time_utc", "")
        start_ts = datetime.datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp()
        return min(start_ts + GAME_SECONDS.get(league, DEFAULT_GAME_SECONDS), now_ts)
    except Exception:
        return now_ts


def select_active(entries, final_eligible=None):
    """Pick the winning key from priority-ordered (key, game) pairs.

    entries        : ordered iterable of (key, game_dict_or_None), already in
                     SCOREBOARD_PRIORITY order.
    final_eligible : optional callable(key) -> bool gating which FINAL/OFF games
                     may win.  The display passes its post-game-window check; the
                     web side passes None (any FINAL/OFF is eligible).

    Returns the winning key — first LIVE/CRIT, else first eligible FINAL/OFF — or
    None when nothing belongs on the board.
    """
    entries = list(entries)
    for key, game in entries:
        if game and game.get("state") in LIVE_STATES:
            return key
    for key, game in entries:
        if game and game.get("state") in FINAL_STATES:
            if final_eligible is None or final_eligible(key):
                return key
    return None
