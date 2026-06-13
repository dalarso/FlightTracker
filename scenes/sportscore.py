"""
SportScoreScene — multi-sport live scoreboard for the LED matrix.

Behaviour
---------
* Supports NHL, NFL, NBA, MLB, and MLS simultaneously.  Each sport is
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
  y = 8   TEAM (gold)                OPP (white)
  y = 22  [score] (large or regular)  –  [score]
  y = 31  period / inning / FINAL
"""

import datetime
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
from utilities.scoreboard_select import select_active
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


_last_horn_ping = 0.0


def _send_state(slots, now_ts: float) -> None:
    """Throttled heartbeat that ALSO carries the board's currently-displayed game, so the
    LAN listener can mirror it (score / opponent / horn-log) and reset in lock-step with the
    board's own 30-min post-game window — no separate timer to drift.  Sent from the TOP of
    the keyframe so the heartbeat never stalls during a goal celebration.  Fire-and-forget;
    an offline listener never affects the Pi.  The 'reportable' game mirrors active_slot:
    a LIVE/CRIT game, else a FINAL game still inside POST_GAME_SECONDS; otherwise NONE.
    Wire format: 'STATE|state|team|tscore|opp|oscore|period|gameid'  or  'STATE|NONE'."""
    global _last_horn_ping
    if _horn_sock is None or now_ts - _last_horn_ping < GOAL_HORN_PING_SECS:
        return
    _last_horn_ping = now_ts
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
        msg = "STATE|{}|{}|{}|{}|{}|{}|{}".format(
            _clean(g.get("state", "")), _clean(rep["team_name"]), _clean(g.get("team_score", 0)),
            _clean(g.get("opp_abbr", "?")), _clean(g.get("opp_score", 0)),
            _clean(g.get("period_label", "")), _clean(g.get("game_id", "")))
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
_TEAM_X   = 1
_SEP_X    = 31            # separator centred on the 64 px display (+2 from old 29)

_TEAM_SCORE_RIGHT = 29   # right edge of team-score area (+2 from old 27)
_OPP_SCORE_LEFT   = 38   # left edge of opp-score area  (+2 from old 36)
_SCORE_CHAR_PX    = 9    # approx px per char — 8×13B bold (actual DWIDTH=8; 9 is conservative)

# ── Poll intervals ─────────────────────────────────────────────────────────────
_POLL_LIVE = 10    # seconds — active period
_POLL_IDLE = 120   # seconds — pre/post/intermission


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
        return now_ts + _secs_until_midnight()
    state = game.get("state", "FUT")
    if state in ("LIVE", "CRIT"):
        return now_ts + _POLL_LIVE
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
            if result is not None:
                slot["game"]     = result
                slot["poll_due"] = _next_poll(result, time.time(), slot["game_ended_at"])
        except Exception:
            pass   # keep last-known game; poll_due already scheduled above
        finally:
            slot["_inflight"] = False

    threading.Thread(target=_work, daemon=True).start()


def _score_x(score_str: str, is_team: bool, char_px: int = _SCORE_CHAR_PX) -> int:
    """Right-align team score to centre-left; left-align opp score from centre-right."""
    w = len(score_str) * char_px
    return max(1, _TEAM_SCORE_RIGHT - w) if is_team else _OPP_SCORE_LEFT


def _opp_x(abbr: str) -> int:
    """Right-align opp abbreviation to 1 px from the right edge of the display.
    Small-font chars are 4 px wide + 1 px spacing = 5 px advance;
    total width = 5*n - 1.  Start x = 64 - 5*n  (gives 1 px right margin).
    Handles both 3-char (→ x=49) and 4-char (→ x=44) team abbreviations.
    """
    return max(_SEP_X + 8, 64 - len(abbr) * 6)


def _center_x(text: str, char_w: int = 6) -> int:
    return max(0, (screen.WIDTH - len(text) * char_w) // 2)


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

    # ── Drawing helpers ────────────────────────────────────────────────────────

    def _draw_score(self, slot: dict, game: dict) -> None:
        """Paint the scoreboard for *slot*/*game* onto self.canvas."""
        state     = game.get("state", "")
        opp       = game.get("opp_abbr", "?")
        team_s    = str(game.get("team_score", 0))
        opp_s     = str(game.get("opp_score", 0))
        font      = slot["score_font"]
        char_px   = slot["score_char_px"]
        team_name = slot["team_name"]

        # Abbreviation row
        graphics.DrawText(self.canvas, _ABBR_FONT, _TEAM_X,     _ABBR_Y, TEAM_GOLD,     team_name)
        graphics.DrawText(self.canvas, _ABBR_FONT, _opp_x(opp), _ABBR_Y, colours.WHITE, opp)

        if state in ("LIVE", "CRIT"):
            graphics.DrawText(self.canvas, font,      _score_x(team_s, True,  char_px), _SCORE_Y, TEAM_GOLD,     team_s)
            graphics.DrawText(self.canvas, _SEP_FONT, _SEP_X,                            _SCORE_Y, colours.GREY,  "-")
            graphics.DrawText(self.canvas, font,      _score_x(opp_s,  False, char_px), _SCORE_Y, colours.WHITE, opp_s)
            lbl = game.get("period_label", "")
            graphics.DrawText(self.canvas, _ABBR_FONT, _center_x(lbl), _STATUS_Y, colours.GREY, lbl)

        elif state in ("FINAL", "OFF"):
            won    = game.get("team_score", 0) > game.get("opp_score", 0)
            colour = colours.GREEN if won else TEAM_GOLD
            graphics.DrawText(self.canvas, font,      _score_x(team_s, True,  char_px), _SCORE_Y, colour,        team_s)
            graphics.DrawText(self.canvas, _SEP_FONT, _SEP_X,                            _SCORE_Y, colours.GREY,  "-")
            graphics.DrawText(self.canvas, font,      _score_x(opp_s,  False, char_px), _SCORE_Y, colours.WHITE, opp_s)
            # Use period_label only for MLB-style extra-inning labels ("F/10",
            # "F/12", etc.).  For all other sports, period_label holds the live
            # period name ("3rd", "Q4", "OT") which is NOT appropriate for a
            # FINAL line — build "FINAL" / "FINAL OT" / "FINAL SO" from period_type.
            pl     = game.get("period_label", "")
            pt     = game.get("period_type", "REG")
            if pl.startswith("F/"):       # MLB extra innings only (e.g. "F/10")
                status = pl
            elif pt in ("OT", "SO"):      # OT/SO final — period_label carries the OT-aware
                status = f"FINAL {pl}" if pl else f"FINAL {pt}"   # name: "OT","2OT","SO" → "FINAL 2OT"
            else:
                status = "FINAL"
            graphics.DrawText(self.canvas, _ABBR_FONT, _center_x(status), _STATUS_Y, colours.GREY, status)

    def _reset_sport_draws(self) -> None:
        """Clear per-slot draw keys so sports_score repaints on next tick."""
        for slot in self._sport_slots:
            slot["last_draw"] = None

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
        if len(self._data):
            return

        now_ts = time.time()

        if now_ts >= self._celebration_until:
            if self._goal_celebration_active:
                self._goal_celebration_active = False
                self._reset_sport_draws()
                self.reset_scene()
            return

        # ── First frame: initialise ────────────────────────────────────────────
        if not self._goal_celebration_active:
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

        # Stamp the post-game window start BEFORE _send_state reads it, so a FINAL that
        # actually ended hours ago (e.g. process restarted long after the buzzer) is
        # recognised as already-expired on the very first tick rather than mis-reported as
        # a fresh FINAL to the LAN listener.  The full bookkeeping loop below re-stamps the
        # same way; doing the minimal stamp here just makes _send_state agree with the
        # display's own post-game-window guard on tick 1.
        for slot in self._sport_slots:
            g = slot.get("game")
            if g and g.get("state") in ("FINAL", "OFF") and slot["game_ended_at"] is None:
                slot["game_ended_at"] = now_ts

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
                     if (s.get("game") or {}).get("state") in ("LIVE", "CRIT")),
                    None,
                )
                for slot in self._sport_slots:
                    if now_ts >= slot["poll_due"] and not slot.get("_inflight"):
                        _poll_slot_async(slot, now_ts)
                    # Stamp the post-game window start as soon as a slot shows FINAL.
                    g = slot.get("game")
                    if g and g.get("state") in ("FINAL", "OFF") and slot["game_ended_at"] is None:
                        slot["game_ended_at"] = now_ts
                if horn_slot is not None:
                    g = horn_slot["game"]
                    new_score = g.get("team_score", 0)
                    if (horn_slot["celebrate"]
                            and horn_slot["last_team_score"] is not None
                            and new_score > horn_slot["last_team_score"]):
                        _send_horn("GOAL", horn_slot["team_name"], new_score,
                                   g.get("opp_abbr", ""), g.get("opp_score", 0))
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
                    slot["game_ended_at"] = now_ts
                # One-shot "{team} WINS!" — fires for EVERY sport.  A win happens once per
                # game, so (unlike goal celebrations) it is NOT gated by the per-sport
                # `celebrate` flag — NBA, which mutes its frequent goal celebrations, still
                # gets a win one.  Only for a game we actually watched go LIVE (so a restart
                # INTO a finished game never false-fires), and decoupled from the
                # game_ended_at set above so a buzzer-beater goal celebration can't swallow
                # it.  Reuses the goal-celebration scroll machinery.
                if (slot["was_live"] and not slot["win_shown"]
                        and game.get("team_score", 0) > game.get("opp_score", 0)):
                    self._celebration_until    = now_ts + WIN_CELEBRATION_SECONDS
                    self._celebration_text     = f'{slot["team_name"]} WINS!'
                    self._celebration_scroll_x = screen.WIDTH
                    slot["win_shown"]          = True
                    _send_horn("WIN", slot["team_name"], game.get("team_score", 0),
                               game.get("opp_abbr", ""), game.get("opp_score", 0))   # nudge the LAN listener (win sound)

        # ── Find highest-priority displayable game ─────────────────────────────
        # Shared selection contract with the web side (utilities.scoreboard_select):
        # first LIVE/CRIT, else first FINAL/OFF.  The display additionally gates a
        # FINAL/OFF on its post-game window (the web side leaves that to server.py).
        _slot_by_key = {s["key"]: s for s in self._sport_slots}

        def _final_window_ok(key):
            ended = _slot_by_key[key]["game_ended_at"]
            return bool(ended) and (now_ts - ended) <= POST_GAME_SECONDS

        _winner = select_active(
            [(s["key"], s["game"]) for s in self._sport_slots], _final_window_ok
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
            _send_horn("GOAL", active_slot["team_name"], new_score,
                       game.get("opp_abbr", ""), game.get("opp_score", 0))   # nudge the LAN horn, in sync
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
        )

        slot_changed = (active_slot is not self._active_slot)

        if not self._scoreboard_active or slot_changed or draw_key != active_slot["last_draw"]:
            self._scoreboard_active    = True
            self._active_slot          = active_slot
            active_slot["last_draw"]   = draw_key
            self.reset_scene()
            self._draw_score(active_slot, game)
