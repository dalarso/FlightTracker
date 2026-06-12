"""Scoreboard data layer — extracted from server.py.

Resolves the active game for the configured team across NHL / ESPN (NFL, NBA, MLS) /
MLB and persists the post-game "ended at" timestamp so the 30-min post-game window
survives a web restart.  The sport fetchers import straight from utilities; server.py
injects its read_config (team preferences) via bind().  The four helpers are re-imported
into server.py so the /api/scoreboard handler calls them unchanged.
"""
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from utilities.nhl  import fetch_game      as _sb_fetch_nhl
from utilities.espn import fetch_espn_game as _sb_fetch_espn
from utilities.mlb  import fetch_mlb_game  as _sb_fetch_mlb
from utilities.scoreboard_select import select_active


def read_config():        # replaced by server.py's read_config in bind()
    return {}


def bind(read_config_fn):
    """Inject server.py's read_config (team preferences)."""
    global read_config
    read_config = read_config_fn


# File that persists game_ended_at across web-service restarts (keyed by game_id
# so yesterday's game doesn't suppress today's).
_GAME_ENDED_AT_FILE = Path("/tmp/ft_game_ended_at.json")


def _load_persisted_game_ended_at(game_id) -> float | None:
    """Return the persisted ended_at timestamp for game_id, or None if missing/stale."""
    try:
        data = json.loads(_GAME_ENDED_AT_FILE.read_text())
        if data.get("game_id") == game_id:
            return float(data["ended_at"])
    except Exception:
        pass
    return None


def _persist_game_ended_at(game_id, ended_at: float) -> None:
    """Write game_ended_at to disk so it survives web-service restarts."""
    try:
        tmp = Path(str(_GAME_ENDED_AT_FILE) + ".tmp")
        tmp.write_text(json.dumps({"game_id": game_id, "ended_at": ended_at}))
        tmp.replace(_GAME_ENDED_AT_FILE)
    except Exception:
        pass


def _clear_persisted_game_ended_at() -> None:
    """Remove the persisted ended_at file (game is no longer final)."""
    _GAME_ENDED_AT_FILE.unlink(missing_ok=True)


def _fetch_scoreboard_data():
    """
    Fetch today's highest-priority active game across all configured sports.
    Returns (game, team_name, sport_key, enabled).
      game       — dict or None
      team_name  — abbreviation for the team with the active game
      sport_key  — e.g. "NHL", "NFL", "MLB", "NBA", "MLS"
      enabled    — True if at least one sport is configured and master switch is on
    """
    cfg = {}
    try:
        cfg = read_config()
    except Exception:
        pass

    # Master switch — default True when key is absent so configs written before
    # this feature was added keep working until the user saves via the web UI.
    if not cfg.get("SCOREBOARD_ENABLED", True):
        return None, "VGK", "NHL", False

    tz_name = cfg.get("TIMEZONE", "America/Los_Angeles")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = None

    # Backward-compat: old SCOREBOARD_ENABLED / SCOREBOARD_TEAM_ID map to NHL
    def _b(key, fallback_key=None, default=None):
        v = cfg.get(key)
        if v is None and fallback_key:
            v = cfg.get(fallback_key)
        return v if v is not None else default

    priority = cfg.get("SCOREBOARD_PRIORITY", ["NHL", "NFL", "MLB", "NBA", "MLS"])

    _SPORT_DEFS = {
        "NHL": {
            "enabled":   bool(_b("SCOREBOARD_NHL_ENABLED", "SCOREBOARD_ENABLED",   True)),
            "team_id":   int(_b("SCOREBOARD_NHL_TEAM_ID",  "SCOREBOARD_TEAM_ID",   0)),
            "team_name": str(_b("SCOREBOARD_NHL_TEAM_NAME","SCOREBOARD_TEAM_NAME", "")),
            "fetch_fn":  lambda tid: _sb_fetch_nhl(tid, tz),
        },
        "NFL": {
            "enabled":   bool(cfg.get("SCOREBOARD_NFL_ENABLED", False)),
            "team_id":   int(cfg.get("SCOREBOARD_NFL_TEAM_ID",  0)),
            "team_name": str(cfg.get("SCOREBOARD_NFL_TEAM_NAME", "")),
            "fetch_fn":  lambda tid: _sb_fetch_espn("football/nfl", tid, tz),
        },
        "MLB": {
            "enabled":   bool(cfg.get("SCOREBOARD_MLB_ENABLED", False)),
            "team_id":   int(cfg.get("SCOREBOARD_MLB_TEAM_ID",  0)),
            "team_name": str(cfg.get("SCOREBOARD_MLB_TEAM_NAME", "")),
            "fetch_fn":  lambda tid: _sb_fetch_mlb(tid, tz),
        },
        "NBA": {
            "enabled":   bool(cfg.get("SCOREBOARD_NBA_ENABLED", False)),
            "team_id":   int(cfg.get("SCOREBOARD_NBA_TEAM_ID",  0)),
            "team_name": str(cfg.get("SCOREBOARD_NBA_TEAM_NAME", "")),
            "fetch_fn":  lambda tid: _sb_fetch_espn("basketball/nba", tid, tz),
        },
        "MLS": {
            "enabled":   bool(cfg.get("SCOREBOARD_MLS_ENABLED", False)),
            "team_id":   int(cfg.get("SCOREBOARD_MLS_TEAM_ID",  0)),
            "team_name": str(cfg.get("SCOREBOARD_MLS_TEAM_NAME", "")),
            "fetch_fn":  lambda tid: _sb_fetch_espn("soccer/usa.1", tid, tz),
        },
    }

    any_enabled = any(
        _SPORT_DEFS[k]["enabled"] and _SPORT_DEFS[k]["team_id"]
        for k in priority if k in _SPORT_DEFS
    )

    # Fetch each enabled sport once (priority order), then apply the SHARED selection
    # contract (utilities.scoreboard_select.select_active): first LIVE/CRIT, else first
    # FINAL/OFF.  No post-game window here — server.py applies it via the persisted
    # ended-at file (the documented divergence from the LED display, which gates inline).
    fetched = {}   # league -> (game_or_None, team_name)
    for league in priority:
        spec = _SPORT_DEFS.get(league)
        if not spec or not spec["enabled"] or not spec["team_id"]:
            continue
        try:
            game = spec["fetch_fn"](spec["team_id"])
        except Exception:
            game = None
        fetched[league] = (game, spec["team_name"])

    entries = [(league, fetched[league][0]) for league in priority if league in fetched]
    winner = select_active(entries)
    if winner is not None:
        game, team_name = fetched[winner]
        return game, team_name, winner, any_enabled

    # No active game — return None so the cache signals no scoreboard
    return None, "VGK", "NHL", any_enabled
