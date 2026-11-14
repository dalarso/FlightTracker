"""
SportScoreScene — multi-sport live scoreboard for the LED matrix.

Behaviour
---------
* Supports NHL, NFL, MLB, NBA, WNBA, MLS, and the FIFA World Cup.  Each sport is
  configured independently with its own team ID and can be enabled or
  disabled without affecting the others.
* While a configured team's game is LIVE (or just finished, within the
  post-game window) the scene takes over the full idle display, suppressing
  the clock, date, day, temperature, and rainfall.  Pre-game hours stay on
  the normal idle scenes — the board switches over once play begins.
* If multiple sports are active at the same time, the highest-priority live
  game wins.  Priority order is defined by SCOREBOARD_PRIORITY in config.py.
  LIVE/CRIT games always beat a post-game FINAL from a lower-priority sport.
* Flights always take priority over the scoreboard.
* After a score increase a full-screen celebration scrolls for
  SCOREBOARD_GOAL_CELEBRATION_SECONDS.  NBA is excluded (scores too frequently).
* The scoreboard stays visible for SCOREBOARD_POST_GAME_MINUTES after a game
  ends, then idle scenes resume.

APIs
----
* NHL  — official NHL Stats API  (api-web.nhle.com)
* MLB  — official MLB Stats API  (statsapi.mlb.com)
* NFL  — ESPN unofficial API     (site.api.espn.com)
* NBA  — ESPN unofficial API
* MLS  — ESPN unofficial API

Poll cadence (per sport slot)
-----------------------------
* 10 s while a period is live
* 2 min during pre-game, intermissions, and post-game
* FUT  → sleep until 30 min before start time, then 2 min
* None → sleep until 12:05 AM (date rollover is the only reason to recheck)

Display layout (64 × 32 LED matrix)
------------------------------------
Standard scoreboard orientation: AWAY on the left, HOME on the right.  The configured team
keeps its gold highlight on whichever side it plays; the opponent is white.
  y = 7   AWAY abbr                  HOME abbr
  y = 20  [away score]      –      [home score]
  y = 31  period / inning / FINAL
"""

import datetime
import json
import socket
import sys
import threading
import time
from functools import partial
from zoneinfo import ZoneInfo

from utilities.animator import Animator
from utilities.nhl  import fetch_game   as _fetch_nhl
from utilities.espn import fetch_espn_game
from utilities.mlb  import fetch_mlb_game as _fetch_mlb
from utilities.scoreboard_select import (select_active, estimate_ended_at as _estimate_ended_at,
                                         GAME_SECONDS as _GAME_SECONDS,
                                         DEFAULT_GAME_SECONDS as _DEFAULT_GAME_SECONDS)
from setup import colours, fonts, frames, screen
from rgbmatrix import graphics

# ── Per-sport ESPN fetch wrappers (same (team_id, tz) signature as NHL/MLB) ──
_fetch_nfl  = partial(fetch_espn_game, "football/nfl")
_fetch_nba  = partial(fetch_espn_game, "basketball/nba")
_fetch_wnba = partial(fetch_espn_game, "basketball/wnba")     # WNBA (same ESPN shape as the NBA)
_fetch_mls  = partial(fetch_espn_game, "soccer/usa.1")
_fetch_fifa = partial(fetch_espn_game, "soccer/fifa.world")   # FIFA World Cup (national teams)


# ── Timezone ──────────────────────────────────────────────────────────────────
try:
    from config import TIMEZONE
except (ImportError, NameError):
    TIMEZONE = "America/Los_Angeles"

try:
    _TZ = ZoneInfo(TIMEZONE)
except Exception:
    _TZ = ZoneInfo("America/Los_Angeles")


# ── Shared scoreboard settings ────────────────────────────────────────────────
def _clog(msg: str) -> None:
    """Timestamped line in plane.log (same shape as overhead._log) for the celebration
    lifecycle — arm / show / suspend / end.  Celebrations used to leave no trace, so the
    2026-06-25 and 2026-10-08 blank-panel hangs could only be reconstructed afterwards."""
    ts = datetime.datetime.now(_TZ).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [sportscore] {msg}", flush=True)


def _cfg(name, default):
    try:
        import config
        return getattr(config, name, default)
    except (ImportError, AttributeError):
        return default

POST_GAME_SECONDS        = int(_cfg("SCOREBOARD_POST_GAME_MINUTES",        30)) * 60
GOAL_CELEBRATION_SECONDS = int(_cfg("SCOREBOARD_GOAL_CELEBRATION_SECONDS", 30))
WIN_CELEBRATION_SECONDS  = int(_cfg("SCOREBOARD_WIN_CELEBRATION_SECONDS",  180))  # 3 min "{team} WINS!"

# ── Optional LAN goal-horn nudge ────────────────────────────────────────────────
# When SCOREBOARD_GOAL_HORN_HOST is set in config.py (e.g. "192.168.1.30"), a fire-and-
# forget UDP packet is sent to that machine the instant the matrix shows "{team} GOAL!".
# A tiny listener there plays a goal-horn .wav/.mp3 in sync with the board.  Empty = off.
GOAL_HORN_HOST = str(_cfg("SCOREBOARD_GOAL_HORN_HOST", "")).strip()
GOAL_HORN_PORT = int(_cfg("SCOREBOARD_GOAL_HORN_PORT", 50505))
GOAL_HORN_PING_SECS = int(_cfg("SCOREBOARD_GOAL_HORN_PING_SECS", 5))  # heartbeat → app shows "connected"
# The listener is the NHL team's goal horn (remote-agents/goal-horn — the VGK app), so ONLY
# the NHL slot drives it: GOAL/WIN nudges and the STATE mirror alike.  Every other sport
# still celebrates on the panel; it just never sounds a hockey horn on the desktop.
GOAL_HORN_LEAGUE = "NHL"

_horn_sock = None
if GOAL_HORN_HOST:
    try:
        _horn_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _horn_sock.setblocking(False)
    except Exception:
        _horn_sock = None


def _clean(s) -> str:
    """Make a field safe for the pipe-delimited wire format (matches planeding._clean):
    a team/opponent/period string containing '|' would otherwise desync the listener's
    positional parse."""
    return str(s if s is not None else "").replace("|", "/").replace("\n", " ").strip()


def _send_horn(kind: str, team: str, tscore, opp, oscore) -> None:
    """Fire-and-forget UDP nudge ('GOAL'/'WIN') to the LAN horn listener, sent the instant
    the matrix lights up the matching celebration.  Carries the score so the listener can
    log it.  NON-BLOCKING and fully swallowed: if the listener PC is off/closed/unreachable
    the packet just vanishes and the display is unaffected.  The listener picks the sound.
    Wire format: 'GOAL|team|tscore|opp|oscore'  (or 'WIN|…')."""
    if _horn_sock is None:
        return
    try:
        msg = "{}|{}|{}|{}|{}".format(_clean(kind), _clean(team), _clean(tscore),
                                      _clean(opp), _clean(oscore))
        _horn_sock.sendto(msg.encode("utf-8", "replace"), (GOAL_HORN_HOST, GOAL_HORN_PORT))
    except Exception:
        pass


def _nudge_horn(kind: str, slot, game, tscore) -> None:
    """Send a GOAL/WIN horn nudge for `slot`'s game — but only when it is the
    GOAL_HORN_LEAGUE (NHL) slot.  Other sports' goals/wins stay on the panel only."""
    if slot.get("key") != GOAL_HORN_LEAGUE:
        return
    _send_horn(kind, slot["team_name"], tscore, game.get("opp_abbr", ""), game.get("opp_score", 0))


_last_horn_ping = 0.0


def _send_state(slots, now_ts: float) -> None:
    """Throttled heartbeat that ALSO carries the board's currently-displayed game, so the
    LAN listener can mirror it (score / opponent / horn-log) and reset in lock-step with the
    board's own 30-min post-game window — no separate timer to drift.  Sent from the TOP of
    the keyframe so the heartbeat never stalls during a goal celebration.  Fire-and-forget;
    an offline listener never affects the Pi.  Only the GOAL_HORN_LEAGUE (NHL) slot is
    reportable — the listener is the NHL horn, so another sport's game never shows there:
    its LIVE/CRIT game, else its FINAL game still inside POST_GAME_SECONDS; otherwise NONE
    (still sent, so the heartbeat keeps the app "connected").
    Wire format: 'STATE|state|team|tscore|opp|oscore|period|gameid|venue'  or  'STATE|NONE'.
    venue is 'H' (team at home) / 'A' (away) so the listener can draw away-left/home-right
    like the board; it is the LAST field, so an older listener (which reads 7) ignores it."""
    global _last_horn_ping
    if _horn_sock is None or now_ts - _last_horn_ping < GOAL_HORN_PING_SECS:
        return
    _last_horn_ping = now_ts
    slots = [s for s in slots if s.get("key") == GOAL_HORN_LEAGUE]
    rep = None
    for slot in slots:
        g = slot.get("game")
        if g and g.get("state") in ("LIVE", "CRIT"):
            rep = slot
            break
    if rep is None:
        for slot in slots:
            g = slot.get("game")
            e = slot.get("game_ended_at")
            # game_ended_at is stamped just BEFORE this call in _sports_score, so a genuinely
            # just-ended game already has e set to ~now and reports FINAL here.  Require a
            # stamped, in-window end (mirrors the display's own guard) so a stale FINAL the
            # process restarted INTO — where stamping is deferred to this same tick but the
            # buzzer was hours ago — is treated as out-of-window (e set, now - e > window)
            # rather than mis-reported as a fresh FINAL.  e is None only transiently (no
            # game / pre-stamp) → not reportable.
            if g and g.get("state") in ("FINAL", "OFF") and e is not None and now_ts - e <= POST_GAME_SECONDS:
                rep = slot
                break
    if rep is not None:
        g = rep["game"]
        msg = "STATE|{}|{}|{}|{}|{}|{}|{}|{}".format(
            _clean(g.get("state", "")), _clean(rep["team_name"]), _clean(g.get("team_score", 0)),
            _clean(g.get("opp_abbr", "?")), _clean(g.get("opp_score", 0)),
            _clean(g.get("period_label", "")), _clean(g.get("game_id", "")),
            "H" if g.get("team_home") else "A")
    else:
        msg = "STATE|NONE"
    try:
        _horn_sock.sendto(msg.encode("utf-8", "replace"), (GOAL_HORN_HOST, GOAL_HORN_PORT))
    except Exception:
        pass

# Master switch — default True when absent so pre-existing configs keep working
# until the user saves once via the web UI (which then writes SCOREBOARD_ENABLED).
SCOREBOARD_MASTER_ENABLED = bool(_cfg("SCOREBOARD_ENABLED", True))

# Priority order — first entry wins when multiple sports are active
_DEFAULT_PRIORITY = ["NHL", "NFL", "MLB", "NBA", "WNBA", "MLS", "FIFA"]
SCOREBOARD_PRIORITY = _cfg("SCOREBOARD_PRIORITY", _DEFAULT_PRIORITY)


# ── Per-sport config helpers ───────────────────────────────────────────────────
def _sport_cfg(league: str, key: str, default):
    """Read SCOREBOARD_{LEAGUE}_{KEY} from config, fall back to *default*."""
    return _cfg(f"SCOREBOARD_{league}_{key}", default)


def _nhl_enabled():
    # Backward compat: old single-sport SCOREBOARD_ENABLED mapped to NHL enabled
    v = _cfg("SCOREBOARD_NHL_ENABLED", None)
    if v is not None:
        return bool(v)
    v = _cfg("SCOREBOARD_ENABLED", None)
    return bool(v) if v is not None else True

def _nhl_team_id():
    v = _cfg("SCOREBOARD_NHL_TEAM_ID", None)
    if v is not None:
        return int(v)
    return int(_cfg("SCOREBOARD_TEAM_ID", 54))

def _nhl_team_name():
    v = _cfg("SCOREBOARD_NHL_TEAM_NAME", None)
    if v is not None:
        return str(v)
    return str(_cfg("SCOREBOARD_TEAM_NAME", "VGK"))


# ── Display constants ─────────────────────────────────────────────────────────
TEAM_GOLD = graphics.Color(200, 160, 30)

_ABBR_FONT     = fonts.regular    # 6×12 — abbreviations & status line
_SEP_FONT      = fonts.small      # 5×8  — "-" separator
_SCORE_FONT    = fonts.large_bold # 8×13B — scores, all sports (incl. NBA 3-digit)
_CELE_FONT     = fonts.huge       # 10×20 — celebration text

_ABBR_Y   = 7    # 6×12: first visible row = baseline-6; baseline 7 → 1 px top margin
_SCORE_Y  = 20   # centred between abbr (bottom y=7) and period (top y=25): 2 px gap each side
_STATUS_Y = 31
_LEFT_X   = 1             # left (away) abbreviation
_SEP_X    = 31            # separator centred on the 64 px display (+2 from old 29)

_LEFT_SCORE_RIGHT = 29   # right edge of the left (away) score area
_RIGHT_SCORE_LEFT = 38   # left edge of the right (home) score area
_SCORE_CHAR_PX    = 9    # approx px per char — 8×13B bold (actual DWIDTH=8; 9 is conservative)
_ABBR_CHAR_PX     = 6    # 6×12 abbreviation font advance

# ── Poll intervals ─────────────────────────────────────────────────────────────
_POLL_LIVE = 10    # seconds — active period
_POLL_IDLE = 120   # seconds — pre/post/intermission
_POLL_NO_GAME = 3600   # seconds — no game known yet (fetch failed or none today): re-check hourly


# ── Helper functions ──────────────────────────────────────────────────────────

def _secs_until_midnight() -> float:
    """Seconds until 12:05 AM local time — one check per no-game day."""
    now      = datetime.datetime.now(_TZ)
    tomorrow = (now + datetime.timedelta(days=1)).replace(
        hour=0, minute=5, second=0, microsecond=0
    )
    return max(60.0, (tomorrow - now).total_seconds())


_pregame_parse_warned: set = set()


def _secs_until_pregame(start_time_utc: str) -> float:
    """Seconds until 30 min before scheduled start (0.0 if already past).

    On a parse failure return 0.0 — the caller's _POLL_IDLE clamp keeps polling at the
    2-min cadence regardless, so the return value is unchanged in practice.  We log the
    failing value ONCE (deduped) so a permanent upstream schema change surfaces in the
    log instead of silently degrading to constant 2-min polling forever."""
    try:
        utc_dt  = datetime.datetime.fromisoformat(start_time_utc.replace("Z", "+00:00"))
        pregame = utc_dt - datetime.timedelta(minutes=30)
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        return max(0.0, (pregame - now_utc).total_seconds())
    except Exception as e:
        if start_time_utc not in _pregame_parse_warned:
            _pregame_parse_warned.add(start_time_utc)
            print(f"[sportscore] could not parse start_time_utc {start_time_utc!r}: {e} "
                  f"(falling back to {_POLL_IDLE}s polling)", file=sys.stderr, flush=True)
        return 0.0


def _next_poll(game: dict | None, now_ts: float, ended_at: float | None = None) -> float:
    """Return the timestamp of the next poll for a slot given its current game."""
    if game is None:
        # No game known yet.  The fetchers return None both for "no game today" and for a
        # transport/HTTP error, so re-check at least hourly: sleeping to midnight here meant
        # ONE failed fetch right after a restart hid that sport's board and horn all day.
        return now_ts + min(_secs_until_midnight(), _POLL_NO_GAME)
    state = game.get("state", "FUT")
    if state in ("LIVE", "CRIT"):
        return now_ts + _POLL_LIVE
    if state == "PPD":
        # Postponed / cancelled / suspended: nothing to show, but re-check hourly in case the
        # provider corrects it (or a doubleheader's other game is still to come).
        return now_ts + min(_secs_until_midnight(), _POLL_NO_GAME)
    if state == "FUT":
        secs = _secs_until_pregame(game.get("start_time_utc", ""))
        return now_ts + (secs if secs > _POLL_IDLE else _POLL_IDLE)
    if state in ("FINAL", "OFF"):
        # Once the post-game display window has lapsed there is nothing left to show
        # today — stop polling the API every 2 min and sleep until the date rolls over.
        # (Without this a finished game kept hitting the API every 120 s until midnight.)
        if ended_at is not None and (now_ts - ended_at) > POST_GAME_SECONDS:
            return now_ts + _secs_until_midnight()
        return now_ts + _POLL_IDLE
    return now_ts + _POLL_IDLE


def _poll_slot_async(slot: dict, now_ts: float) -> None:
    """Fetch this slot's game on a background daemon thread — NEVER on the render thread.

    A slow/hung sports API used to freeze the whole matrix because the fetch ran inline in
    the keyframe.  Here the render thread only reads slot['game']; the worker swaps in the
    new dict (a single atomic assignment).  poll_due is scheduled up-front so a hung fetch
    can't cause a tight re-poll loop, then refined once the result lands.  The _inflight
    guard prevents overlapping fetches for the same slot (and any thread leak)."""
    if slot.get("_inflight"):
        return
    slot["_inflight"] = True
    slot["poll_due"]  = _next_poll(slot["game"], now_ts, slot["game_ended_at"])
    team_id, fetch_fn = slot["team_id"], slot["fetch_fn"]

    def _work():
        try:
            result = fetch_fn(team_id, _TZ)
        except Exception:
            result = None   # keep last-known game
        try:
            if result is not None:
                slot["game"]     = result
                slot["poll_due"] = _next_poll(result, time.time(), slot["game_ended_at"])
            else:
                # Error or "no game": the up-front schedule may be a long sleep (to 00:05,
                # or to a pre-game window computed from yesterday's game) — never wait
                # longer than _POLL_NO_GAME on a fetch that returned nothing.
                slot["poll_due"] = min(slot["poll_due"], time.time() + _POLL_NO_GAME)
        except Exception:
            pass
        finally:
            slot["_inflight"] = False

    try:
        threading.Thread(target=_work, daemon=True).start()
    except Exception:
        # Couldn't spawn the worker (e.g. thread exhaustion): clear the guard so the next
        # tick retries — otherwise _inflight stays True and this sport never polls again.
        slot["_inflight"] = False
        raise


def _score_x(score_str: str, left: bool, char_px: int = _SCORE_CHAR_PX) -> int:
    """Right-align the left (away) score to centre-left; left-align the right (home) score
    from centre-right, so both hug the separator whatever their digit count."""
    w = len(score_str) * char_px
    return max(1, _LEFT_SCORE_RIGHT - w) if left else _RIGHT_SCORE_LEFT


def _right_abbr_x(abbr: str) -> int:
    """Right-align the right-hand (home) abbreviation against the display's right edge.
    The 6×12 font advances 6 px per char, so x = WIDTH - 6n: 3 chars → x=46, 4 chars → x=40.
    Clamped so a long name never starts left of the separator column."""
    return max(_SEP_X + 8, screen.WIDTH - len(abbr) * _ABBR_CHAR_PX)


def _center_x(text: str, char_w: int = 6) -> int:
    return max(0, (screen.WIDTH - len(text) * char_w) // 2)


def _team_won(game: dict) -> bool:
    """Did the configured team win this finished game?  Prefers the fetcher's explicit
    team_won flag (ESPN marks the winner — the only way to see a penalty-shootout win, where
    the scores are level) and falls back to comparing scores."""
    won = game.get("team_won")
    if won is not None:
        return bool(won)
    return game.get("team_score", 0) > game.get("opp_score", 0)


_STATUS_MAX_CHARS = 64 // 6   # the 6×12 status font fits 10 chars across the 64 px panel


def _live_status(game: dict) -> str:
    """Status line for a LIVE/CRIT game: the period plus the game clock when there is one —
    "2nd 14:32", "OT 3:12", "Q4 0:45", "2nd 67'".  Falls back to the bare period label in an
    intermission / at a period break (the label already says "1st INT", "HALF", "Q2 END"),
    when the clock is stopped at zero (period over, shootout), for clockless sports (MLB),
    or when the combined text wouldn't fit the panel.  The clock refreshes with the 10 s live
    poll, so it steps rather than ticks — the period alone used to be all the board showed."""
    label = game.get("period_label", "") or ""
    clock = str(game.get("time_remaining") or "").strip()
    if not clock or game.get("in_intermission"):
        return label
    if len(clock) == 5 and clock[0] == "0" and clock[2] == ":":
        clock = clock[1:]                       # NHL "04:10" → "4:10"
    if clock.strip("0:.") == "":
        return label                            # "00:00" / "0:00" / "0.0" — nothing to add
    text = f"{label} {clock}" if label else clock
    return text if len(text) <= _STATUS_MAX_CHARS else label


def _final_status(game: dict) -> str:
    """Status line for a FINAL/OFF game: "FINAL", "FINAL OT", "FINAL 2OT", "FINAL SO",
    "FINAL AET", "FINAL PEN", or MLB's extra-inning "F/10".

    period_label is used only for MLB-style extra-inning labels ("F/10") and for the OT/SO
    suffix; for a regulation final it holds the live period name ("3rd", "Q4"), which is not
    appropriate on a FINAL line."""
    pl = game.get("period_label", "")
    pt = game.get("period_type", "REG")
    if pl.startswith("F/"):       # MLB extra innings only (e.g. "F/10")
        return pl
    if pt in ("OT", "SO"):        # period_label carries the OT-aware name: "OT","2OT","SO","AET"
        return f"FINAL {pl}" if pl else f"FINAL {pt}"
    return "FINAL"


def _build_slots() -> list[dict]:
    """
    Build the ordered list of sport slots from config.

    Each slot holds both its static config and its mutable runtime state so
    all per-sport tracking (poll timers, last score, etc.) lives in one place.
    Returns an empty list when the master scoreboard switch is off.
    """
    if not SCOREBOARD_MASTER_ENABLED:
        return []

    all_slots = {
        "NHL": {
            "key":              "NHL",
            "enabled":          _nhl_enabled(),
            "team_id":          _nhl_team_id(),
            "team_name":        _nhl_team_name(),
            "fetch_fn":         _fetch_nhl,
            "score_font":       _SCORE_FONT,
            "score_char_px":    _SCORE_CHAR_PX,
            "celebrate":        True,
            "celebration_text": "{team} GOAL!",
        },
        "NFL": {
            "key":              "NFL",
            "enabled":          bool(_sport_cfg("NFL", "ENABLED", False)),
            "team_id":          int(_sport_cfg("NFL", "TEAM_ID", 0)),
            "team_name":        str(_sport_cfg("NFL", "TEAM_NAME", "")),
            "fetch_fn":         _fetch_nfl,
            "score_font":       _SCORE_FONT,
            "score_char_px":    _SCORE_CHAR_PX,
            "celebrate":        True,
            "celebration_text": "{team} SCORE!",
        },
        "MLB": {
            "key":              "MLB",
            "enabled":          bool(_sport_cfg("MLB", "ENABLED", False)),
            "team_id":          int(_sport_cfg("MLB", "TEAM_ID", 0)),
            "team_name":        str(_sport_cfg("MLB", "TEAM_NAME", "")),
            "fetch_fn":         _fetch_mlb,
            "score_font":       _SCORE_FONT,
            "score_char_px":    _SCORE_CHAR_PX,
            "celebrate":        True,
            "celebration_text": "{team} SCORES!",
        },
        "NBA": {
            "key":              "NBA",
            "enabled":          bool(_sport_cfg("NBA", "ENABLED", False)),
            "team_id":          int(_sport_cfg("NBA", "TEAM_ID", 0)),
            "team_name":        str(_sport_cfg("NBA", "TEAM_NAME", "")),
            "fetch_fn":         _fetch_nba,
            "score_font":       _SCORE_FONT,
            "score_char_px":    _SCORE_CHAR_PX,
            "celebrate":        False,           # scores too frequently
            "celebration_text": "",
        },
        "WNBA": {
            "key":              "WNBA",
            "enabled":          bool(_sport_cfg("WNBA", "ENABLED", False)),
            "team_id":          int(_sport_cfg("WNBA", "TEAM_ID", 0)),
            "team_name":        str(_sport_cfg("WNBA", "TEAM_NAME", "")),
            "fetch_fn":         _fetch_wnba,
            "score_font":       _SCORE_FONT,
            "score_char_px":    _SCORE_CHAR_PX,
            "celebrate":        False,           # scores too frequently (same as the NBA)
            "celebration_text": "",
        },
        "MLS": {
            "key":              "MLS",
            "enabled":          bool(_sport_cfg("MLS", "ENABLED", False)),
            "team_id":          int(_sport_cfg("MLS", "TEAM_ID", 0)),
            "team_name":        str(_sport_cfg("MLS", "TEAM_NAME", "")),
            "fetch_fn":         _fetch_mls,
            "score_font":       _SCORE_FONT,
            "score_char_px":    _SCORE_CHAR_PX,
            "celebrate":        True,
            "celebration_text": "{team} GOAL!",
        },
        "FIFA": {
            "key":              "FIFA",
            "enabled":          bool(_sport_cfg("FIFA", "ENABLED", False)),
            "team_id":          int(_sport_cfg("FIFA", "TEAM_ID", 0)),
            "team_name":        str(_sport_cfg("FIFA", "TEAM_NAME", "")),
            "fetch_fn":         _fetch_fifa,
            "score_font":       _SCORE_FONT,
            "score_char_px":    _SCORE_CHAR_PX,
            "celebrate":        True,
            "celebration_text": "{team} GOAL!",
        },
    }

    # Runtime state template (added to each slot)
    _runtime = {
        "game":            None,   # latest game dict (or None)
        "poll_due":        0.0,    # unix timestamp of next API call
        "game_ended_at":   None,   # when FINAL/OFF was first seen
        "last_team_score": None,   # for goal/score detection
        "last_draw":       None,   # change-detection tuple
        "was_live":        False,  # saw this game LIVE/CRIT — gates the WIN celebration
        "win_shown":       False,  # WIN celebration already fired (one-shot per game)
        "last_game_id":    None,   # game_id of the game whose state we're tracking
        "_inflight":       False,  # a background fetch is in progress for this slot
    }

    ordered = []
    for league in SCOREBOARD_PRIORITY:
        slot = all_slots.get(league.upper())
        if slot and slot["enabled"] and slot["team_id"]:
            ordered.append({**slot, **_runtime})

    return ordered


# ── Web "hide this game" marker ─────────────────────────────────────────────────
# The web UI writes /tmp/ft_dismissed_game = {"game_id": "<id>", "ts": <unix>} when the user
# hides the game currently on the board.  The display suppresses ONLY that game_id, so the
# scoreboard auto-returns for the next game (a different game_id) with nothing to switch back
# on.  The web owns the file (creates/removes it, world-readable 0644); the display only reads
# it — mirrors the pause/night flag ownership so /tmp's sticky bit is never in play.
DISMISS_FILE = "/tmp/ft_dismissed_game"


def _read_dismissed_game_id():
    """Return the hidden game_id (str) from DISMISS_FILE, or None if absent/empty/malformed."""
    try:
        with open(DISMISS_FILE) as f:
            gid = json.load(f).get("game_id")
        gid = str(gid) if gid is not None else ""
        return gid or None
    except Exception:
        return None


# Restart-into-a-final end-time estimate (_estimate_ended_at / _GAME_SECONDS) now lives in
# utilities.scoreboard_select so the web UI uses the same numbers — imported above.


# ── Scene ─────────────────────────────────────────────────────────────────────

class SportScoreScene(object):
    def __init__(self):
        super().__init__()
        self._sport_slots          = _build_slots()
        self._scoreboard_active    = False  # True → idle scenes suppress themselves
        self._active_slot          = None   # slot currently on display
        self._goal_celebration_active   = False
        self._celebration_until    = 0.0
        self._celebration_text     = ""
        self._celebration_scroll_x = screen.WIDTH
        self._celebration_tw       = 0      # cached text width for wrap
        self._celebration_game_id  = None   # game_id that armed the current celebration
        self._dismissed_game_id    = None   # web "hide this game" marker (by game_id)

    # ── Drawing helpers ────────────────────────────────────────────────────────

    def _draw_score(self, slot: dict, game: dict) -> None:
        """Paint the scoreboard for *slot*/*game* onto self.canvas.

        Standard orientation — AWAY left, HOME right: a road game reads "VGK 3 - 2 EDM", a
        home game "EDM 2 - 3 VGK".  The configured team keeps its gold highlight (green score
        on a FINAL it won) on whichever side it plays; the opponent is white.
        """
        state     = game.get("state", "")
        live      = state in ("LIVE", "CRIT")
        final     = state in ("FINAL", "OFF")
        font      = slot["score_font"]
        char_px   = slot["score_char_px"]

        team_score_colour = colours.GREEN if (final and _team_won(game)) else TEAM_GOLD
        # (abbreviation, score text, abbreviation colour, score colour)
        team = (slot["team_name"], str(game.get("team_score", 0)), TEAM_GOLD, team_score_colour)
        opp  = (game.get("opp_abbr", "?"), str(game.get("opp_score", 0)), colours.WHITE, colours.WHITE)
        left, right = (opp, team) if game.get("team_home") else (team, opp)

        # Abbreviation row
        graphics.DrawText(self.canvas, _ABBR_FONT, _LEFT_X,                 _ABBR_Y, left[2],  left[0])
        graphics.DrawText(self.canvas, _ABBR_FONT, _right_abbr_x(right[0]), _ABBR_Y, right[2], right[0])

        if not (live or final):
            return
        graphics.DrawText(self.canvas, font,      _score_x(left[1],  True,  char_px), _SCORE_Y, left[3],      left[1])
        graphics.DrawText(self.canvas, _SEP_FONT, _SEP_X,                             _SCORE_Y, colours.GREY, "-")
        graphics.DrawText(self.canvas, font,      _score_x(right[1], False, char_px), _SCORE_Y, right[3],     right[1])
        status = _live_status(game) if live else _final_status(game)
        graphics.DrawText(self.canvas, _ABBR_FONT, _center_x(status), _STATUS_Y, colours.GREY, status)

    def _reset_sport_draws(self) -> None:
        """Clear per-slot draw keys so sports_score repaints on next tick."""
        for slot in self._sport_slots:
            slot["last_draw"] = None

    def _ended_at_for(self, slot, now_ts):
        """When to record a FINAL/OFF game as having ended.  The accurate `now` if we actually
        watched it finish this session (was_live); otherwise a start-time estimate, so a restart
        INTO an old final doesn't hand it a fresh post-game window (see _estimate_ended_at)."""
        return now_ts if slot["was_live"] else _estimate_ended_at(slot.get("game"), now_ts, slot["key"])

    # ── Reset-time repaint (fires inside reset_scene) ───────────────────────────

    @Animator.KeyFrame.add(0)
    def sport_score_reset(self):
        # Repaint the active board immediately on every reset_scene() so the panel
        # doesn't blank for up to a second (until the 1 Hz sports_score tick) whenever
        # something resets the scene while the board — not a plane — is what's showing:
        # the end of a GOAL/WIN celebration, or an incidental flyover appearing and then
        # departing.  The flight scenes already repaint themselves at reset time (they're
        # divisor-0); the scoreboard was the only active content that didn't, so it dropped
        # to black on each reset and only came back on the next once-a-second tick.  Guards
        # mirror _sports_score: flights win, a live celebration owns the canvas, and we only
        # draw when a slot is genuinely active.  clear_screen sorts before this method, so
        # the canvas is already cleared when we paint on top.
        if len(self._data):
            return
        if self._goal_celebration_active:
            return
        slot = self._active_slot
        if self._scoreboard_active and slot is not None and slot.get("game") is not None:
            self._draw_score(slot, slot["game"])

    # ── Celebration frame (fires every display frame) ──────────────────────────

    @Animator.KeyFrame.add(1)
    def celebration_frame(self, count):
        if not self._sport_slots:
            return

        # Flights always win: while an aircraft is overhead the flight scenes own the
        # canvas, so the celebration must yield exactly like the board does in
        # _sports_score (mirrors the `if len(self._data): return` guard there).  Without
        # this a goal/WIN scroll (up to WIN_CELEBRATION_SECONDS) would blank a live
        # overhead plane — the opposite of the documented priority.  The LAN horn already
        # fired when the celebration armed, so cutting the on-panel scroll short loses no
        # audio cue; the celebration simply doesn't draw while flights are present.
        #
        # SUSPEND, don't just skip: drop the active flag and reset_scene() so the flight
        # scenes' divisor-0 drawers (flight_details / journey / journey_arrow) actually
        # paint — they only draw inside reset_scene().  Returning while the flag stayed
        # True was the 2026-10-08 deadlock: this early return also skipped the expiry
        # cleanup below, nothing drew, plane_details never finished a pass, and with 2+
        # flights grab_new_data stopped polling forever (panel black all night).
        # _celebration_until is left alone, so if the flyover ends inside the window the
        # init branch below re-arms and the scroll resumes; otherwise the board returns.
        if len(self._data):
            if self._goal_celebration_active:
                self._goal_celebration_active = False
                _left = self._celebration_until - time.time()
                _clog(f"celebration {self._celebration_text!r} suspended — {len(self._data)} "
                      f"flight(s) overhead ({max(0, _left):.0f}s of its window left)")
                self._reset_sport_draws()
                self.reset_scene()
            return

        now_ts = time.time()

        if now_ts >= self._celebration_until:
            if self._goal_celebration_active:
                self._goal_celebration_active = False
                _clog(f"celebration {self._celebration_text!r} ended")
                self._reset_sport_draws()
                self.reset_scene()
            return

        # ── First frame: initialise ────────────────────────────────────────────
        if not self._goal_celebration_active:
            _clog(f"celebration {self._celebration_text!r} on screen "
                  f"({self._celebration_until - now_ts:.0f}s left)")
            self._goal_celebration_active   = True
            self._scoreboard_active    = True
            self._celebration_scroll_x = screen.WIDTH
            # Cache text width once so the wrap check is consistent each frame
            self._celebration_tw = graphics.DrawText(
                self.canvas, _CELE_FONT,
                screen.WIDTH * 2, 22,
                TEAM_GOLD,
                self._celebration_text,
            ) or (len(self._celebration_text) * 10)

        # Full-canvas clear
        self.canvas.Clear()

        colour = colours.WHITE if (count % 20 >= 17) else TEAM_GOLD
        graphics.DrawText(
            self.canvas, _CELE_FONT,
            self._celebration_scroll_x, 22,
            colour,
            self._celebration_text,
        )

        self._celebration_scroll_x -= 1
        if self._celebration_scroll_x + self._celebration_tw < 0:
            self._celebration_scroll_x = screen.WIDTH

    # ── Main score keyframe (fires every second) ───────────────────────────────

    @Animator.KeyFrame.add(int(frames.PER_SECOND * 1))
    def sports_score(self, count):
        # Wrapper: a scoreboard data hiccup (e.g. a sports API schema change) must NEVER
        # crash the render loop / display process.  Degrade to "skip this frame" + a
        # deduped stderr line instead.
        try:
            self._sports_score(count)
        except Exception as e:
            if getattr(self, "_sport_last_err", None) != repr(e):
                self._sport_last_err = repr(e)
                print(f"[sportscore] suppressed render error: {e}", file=sys.stderr)

    def _sports_score(self, count):
        if not self._sport_slots:
            return

        now_ts = time.time()

        # ── Web "hide this game" marker ────────────────────────────────────────
        # Suppress ONLY this game_id, so the board auto-returns for the next game.
        dismissed_id = _read_dismissed_game_id()
        self._dismissed_game_id = dismissed_id

        def _is_dismissed(g):
            return bool(dismissed_id) and g is not None and str(g.get("game_id")) == dismissed_id

        # If the game whose GOAL/WIN scroll is currently on screen just got hidden, cut the
        # celebration now instead of letting it run out its 30 s / 180 s (celebration_frame
        # cleans up on its next frame once _celebration_until is in the past).
        if (dismissed_id and self._celebration_game_id is not None
                and str(self._celebration_game_id) == dismissed_id
                and now_ts < self._celebration_until):
            self._celebration_until = 0.0
            _clog(f"celebration {self._celebration_text!r} cut short — game {dismissed_id} hidden")

        # Stamp the post-game window start BEFORE _send_state reads it, so a FINAL that
        # actually ended hours ago (e.g. process restarted long after the buzzer) is
        # recognised as already-expired on the very first tick rather than mis-reported as
        # a fresh FINAL to the LAN listener.  _ended_at_for estimates the real end from the
        # start time for a game we didn't watch finish, so the restart doesn't reset the
        # window.  The full bookkeeping loop below re-stamps the same way.
        for slot in self._sport_slots:
            g = slot.get("game")
            if g and g.get("state") in ("FINAL", "OFF") and slot["game_ended_at"] is None:
                slot["game_ended_at"] = self._ended_at_for(slot, now_ts)

        _send_state(self._sport_slots, now_ts)   # heartbeat + game state → mirrors to the LAN listener

        # ── Flights take priority; keep _scoreboard_active so idle scenes stay ─
        # Checked BEFORE the celebration block so an overhead aircraft is top priority
        # even mid-celebration: celebration_frame yields to flights too, so the flight
        # scenes own the canvas while a plane is overhead.  Background polling/stamping
        # still happens further down once flights clear.
        if len(self._data):
            self._reset_sport_draws()
            return

        # ── Yield canvas to celebration; still poll APIs in background ─────────
        if self._goal_celebration_active:
            if now_ts >= self._celebration_until:
                pass  # celebration_frame will clean up on next tick
            else:
                # Decouple the LAN horn from the on-panel scroll: run a lightweight
                # goal-detection pass for the active LIVE/CRIT slot even mid-celebration so
                # each goal nudges the LAN listener (without re-arming the on-panel scroll).
                horn_slot = next(
                    (s for s in self._sport_slots
                     if (s.get("game") or {}).get("state") in ("LIVE", "CRIT")
                     and not _is_dismissed(s.get("game"))),
                    None,
                )
                for slot in self._sport_slots:
                    if now_ts >= slot["poll_due"] and not slot.get("_inflight"):
                        _poll_slot_async(slot, now_ts)
                    # Stamp the post-game window start as soon as a slot shows FINAL.
                    g = slot.get("game")
                    if g and g.get("state") in ("FINAL", "OFF") and slot["game_ended_at"] is None:
                        slot["game_ended_at"] = self._ended_at_for(slot, now_ts)
                if horn_slot is not None:
                    g = horn_slot["game"]
                    new_score = g.get("team_score", 0)
                    if (horn_slot["celebrate"]
                            and horn_slot["last_team_score"] is not None
                            and new_score > horn_slot["last_team_score"]):
                        _nudge_horn("GOAL", horn_slot, g, new_score)
                    # Advance the baseline so the post-celebration tick doesn't re-fire a
                    # single coalesced scroll for goals already nudged here.
                    if horn_slot["last_team_score"] is not None:
                        horn_slot["last_team_score"] = new_score
            return

        # ── Poll each due slot on a background thread (never blocks the render loop) ─
        # The worker swaps in slot["game"]; the per-slot bookkeeping below runs every tick
        # on whatever game is currently published, so it no longer waits on the network.
        for slot in self._sport_slots:
            if now_ts >= slot["poll_due"] and not slot.get("_inflight"):
                _poll_slot_async(slot, now_ts)

        # ── Update game_ended_at + fire the one-shot WIN celebration ───────────
        for slot in self._sport_slots:
            game = slot["game"]
            if game is None:
                slot["game_ended_at"] = None
                slot["was_live"]      = False
                slot["win_shown"]     = False
                slot["last_game_id"]  = None
                continue
            state = game.get("state", "FUT")
            # New game in the slot (e.g. an MLB doubleheader game 2, or a back-to-back day
            # on a restart) — fully reset the per-game tracking so game 2 isn't poisoned by
            # game 1's frozen win_shown / last_team_score / game_ended_at.  Keyed on game_id,
            # not state, so it fires regardless of was_live.  Guard against the `or 0`
            # fallback ids in the fetchers: a transient parse miss (id 0) must NOT masquerade
            # as a new game and wipe live state — only a real, non-zero, changed id resets.
            gid = game.get("game_id") or 0
            if gid and gid != slot["last_game_id"]:
                if slot["last_game_id"] is not None:
                    slot["was_live"]        = False
                    slot["win_shown"]       = False
                    slot["last_team_score"] = None
                    slot["game_ended_at"]   = None
                slot["last_game_id"] = gid
            # Pre-game baseline so the FIRST goal IS celebrated: capture the score while the
            # game hasn't started (0).  Only for FUT/PRE — a mid-game restart first sees LIVE
            # (last_team_score stays None) so we never falsely celebrate a lead that was
            # already on the board when we joined.  (Now runs every tick — polling is async.)
            if state in ("FUT", "PRE") and slot["last_team_score"] is None:
                slot["last_team_score"] = game.get("team_score", 0)
            if state in ("LIVE", "CRIT"):
                slot["was_live"]      = True
                slot["game_ended_at"] = None
            elif state in ("FUT", "PRE") and not slot["was_live"]:
                # Genuine pre-game (never seen LIVE) — safe to (re)arm baseline.
                # GUARD `not was_live`: ESPN maps a mid-game STATUS_RAIN_DELAY to
                # "PRE", so a game we already watched go LIVE can briefly re-report
                # PRE during a delay.  Without this guard that would wipe was_live /
                # win_shown and suppress the post-resumption WIN celebration.
                slot["was_live"]      = False   # fresh game, not started yet
                slot["win_shown"]     = False
                slot["game_ended_at"] = None
            elif state in ("FINAL", "OFF"):
                if slot["game_ended_at"] is None:
                    slot["game_ended_at"] = self._ended_at_for(slot, now_ts)
                # One-shot "{team} WINS!" — fires for EVERY sport.  A win happens once per
                # game, so (unlike goal celebrations) it is NOT gated by the per-sport
                # `celebrate` flag — NBA, which mutes its frequent goal celebrations, still
                # gets a win one.  Only for a game we actually watched go LIVE (so a restart
                # INTO a finished game never false-fires), and decoupled from the
                # game_ended_at set above so a buzzer-beater goal celebration can't swallow
                # it.  Reuses the goal-celebration scroll machinery.
                if (slot["was_live"] and not slot["win_shown"]
                        and _team_won(game)
                        and not _is_dismissed(game)):
                    self._celebration_until    = now_ts + WIN_CELEBRATION_SECONDS
                    self._celebration_text     = f'{slot["team_name"]} WINS!'
                    self._celebration_scroll_x = screen.WIDTH
                    self._celebration_game_id  = game.get("game_id")
                    slot["win_shown"]          = True
                    _nudge_horn("WIN", slot, game, game.get("team_score", 0))   # LAN win sound (NHL only)
                    _clog(f"celebration armed: {self._celebration_text!r} for {WIN_CELEBRATION_SECONDS}s "
                          f"(game {self._celebration_game_id}, {game.get('team_score', 0)}-{game.get('opp_score', 0)})")

        # ── Find highest-priority displayable game ─────────────────────────────
        # Shared selection contract with the web side (utilities.scoreboard_select):
        # first LIVE/CRIT, else first FINAL/OFF.  The display additionally gates a
        # FINAL/OFF on its post-game window (the web side leaves that to server.py).
        _slot_by_key = {s["key"]: s for s in self._sport_slots}

        def _final_window_ok(key):
            ended = _slot_by_key[key]["game_ended_at"]
            return bool(ended) and (now_ts - ended) <= POST_GAME_SECONDS

        # A hidden game is passed as None so select_active skips it — a lower-priority
        # game (if any) still wins, otherwise the board hands back to the idle scenes.
        _winner = select_active(
            [(s["key"], (None if _is_dismissed(s["game"]) else s["game"]))
             for s in self._sport_slots],
            _final_window_ok,
        )
        active_slot = _slot_by_key.get(_winner)

        # ── Expire post-game slots that have timed out ─────────────────────────
        # IMPORTANT: leave game_ended_at at the real end time.  Pass 2 already stops
        # selecting the slot once (now - ended) exceeds the window; resetting it to None
        # here made the loop above re-arm it to `now` on the very next tick, restarting
        # the 30-min window indefinitely (the FINAL never went idle until midnight).
        for slot in self._sport_slots:
            game = slot["game"]
            if game and game.get("state") in ("FINAL", "OFF"):
                ended = slot["game_ended_at"]
                if ended and now_ts - ended > POST_GAME_SECONDS:
                    slot["last_team_score"] = None
                    slot["last_draw"]       = None

        # ── No active game — hand display back to idle scenes ──────────────────
        if active_slot is None:
            if self._scoreboard_active:
                self._scoreboard_active = False
                self._active_slot       = None
                self.reset_scene()
                self._reset_idle_scenes()  # also calls _reset_sport_draws()
            return

        game  = active_slot["game"]
        state = game.get("state", "")

        # ── Score/goal detection ───────────────────────────────────────────────
        new_score = game.get("team_score", 0)
        if (
            active_slot["celebrate"]
            and active_slot["last_team_score"] is not None
            and state in ("LIVE", "CRIT")
            and new_score > active_slot["last_team_score"]
        ):
            self._celebration_until    = now_ts + GOAL_CELEBRATION_SECONDS
            self._celebration_text     = active_slot["celebration_text"].format(
                team=active_slot["team_name"]
            )
            self._celebration_scroll_x = screen.WIDTH
            self._celebration_game_id  = game.get("game_id")
            _nudge_horn("GOAL", active_slot, game, new_score)   # LAN horn, in sync (NHL only)
            _clog(f"celebration armed: {self._celebration_text!r} for {GOAL_CELEBRATION_SECONDS}s "
                  f"(game {self._celebration_game_id}, {new_score}-{game.get('opp_score', 0)})")
        active_slot["last_team_score"] = new_score

        # Yield to upcoming celebration
        if now_ts < self._celebration_until:
            return

        # ── Change detection & draw ────────────────────────────────────────────
        draw_key = (
            active_slot["key"],
            state,
            new_score,
            game.get("opp_score", 0),
            game.get("opp_abbr", ""),
            game.get("period", 0),
            game.get("period_type", ""),
            game.get("period_label", ""),   # needed: NFL/NBA "Q2 END"→"HALF" share same period+in_int
            game.get("in_intermission", False),
            _live_status(game) if state in ("LIVE", "CRIT") else "",   # game clock on the status line
        )

        slot_changed = (active_slot is not self._active_slot)

        if not self._scoreboard_active or slot_changed or draw_key != active_slot["last_draw"]:
            self._scoreboard_active    = True
            self._active_slot          = active_slot
            self.reset_scene()
            self._draw_score(active_slot, game)
            # Record the key only AFTER a successful paint: if _draw_score raised, the next
            # tick retries instead of leaving the board blank until the score changes.
            active_slot["last_draw"]   = draw_key
