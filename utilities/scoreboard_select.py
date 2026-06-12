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

LIVE_STATES = ("LIVE", "CRIT")
FINAL_STATES = ("FINAL", "OFF")


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
