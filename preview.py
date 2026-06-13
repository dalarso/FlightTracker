#!/usr/bin/env python3
"""FlightTracker desktop preview — a pixel-identical mirror of the LED panel that runs on
ANY machine (e.g. the Windows box next to your horn/ding apps), NOT on the Pi.

It runs the exact same Display (every scene, same fonts/colours/layout) under
RGBMatrixEmulator in a desktop window, and pulls EVERYTHING from the Pi's web API — so
the Pi does no extra work (no flicker), no API keys live on this machine, and what you see
matches what the panel computed:

    flights      GET /api/flights      (the same feed the panel renders)
    weather      GET /api/weather      (current temperature)
    scoreboard   GET /api/scoreboard   (the active game, already prioritised)
    night/pause  GET /api/status
    render cfg   GET /api/config       (timezone, local airports, team name, ...; secrets masked)

Setup (Windows): install Python, `pip install -r requirements-preview.txt`, copy this repo,
then run:  python preview.py        (set FT_PI if the Pi isn't at the default below)

Env:
    FT_PI               base URL of the Pi web UI   (default http://raspberrypi.local:5000)
    FT_PREVIEW_ADAPTER  window adapter (default: auto — 'pygame' if it's installed, for the
                        glowing 'real' LED look in a native window; else 'tkinter' with
                        'circle' dots.  'browser' serves a web/headless view on :8888.)
    FT_PIXEL_STYLE      'real' (glowing LED dots; pygame/browser only), 'circle', or
                        'square'   (default: 'real' on pygame/browser, 'circle' on tkinter)
"""
import json
import os
import sys
import tempfile
import threading
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

import requests

PI = os.environ.get("FT_PI", "http://raspberrypi.local:5000").rstrip("/")


def _auto_adapter():
    """Pick the best window backend available without importing it (no startup banner):
    prefer 'pygame' — a native window with the glowing 'real' LED style — when it's
    installed, else 'tkinter' (native window, 'circle' dots).  Regular pygame has no
    Python-3.14 Windows wheel, but pygame-ce is a drop-in that does: `pip install pygame-ce`
    and the preview upgrades itself to the 'real' look automatically."""
    import importlib.util
    return "pygame" if importlib.util.find_spec("pygame") is not None else "tkinter"


ADAPTER = (os.environ.get("FT_PREVIEW_ADAPTER") or _auto_adapter()).lower()
_HERE = Path(__file__).resolve().parent


def _get(path, default=None, timeout=6):
    try:
        return requests.get(PI + path, timeout=timeout).json()
    except Exception:
        return default


# ── 1. Pull the render config from the Pi → a generated config.py on a temp path ──
# Written to a throwaway dir put first on sys.path, so `from config import X` in the
# scenes resolves to THIS (and never clobbers a real config.py if one's alongside).
def _materialise_config():
    cfg = _get("/api/config", default={}) or {}
    # Secrets are masked by the API and unused here (all data comes from the Pi) — blank them
    # so nothing uses the "********" sentinel as a real key.  ALSO blank the LAN ding/horn
    # hosts: the preview runs the REAL Display, so if these are set it fires its own plane-ding
    # and goal-horn UDP packets to the desktop apps — duplicating the ones the Pi already sends
    # (the double-ding).  Empty host = those emitters are no-ops (planeding.py / sportscore.py).
    for k in ("OPENWEATHER_API_KEY", "OPENSKY_CLIENT_SECRET", "FLIGHTAWARE_API_KEY",
              "AIRLABS_API_KEY", "AIRLABS_API_KEY_2",
              "PLANE_DING_HOST", "SCOREBOARD_GOAL_HORN_HOST"):
        cfg[k] = ""
    workdir = Path(tempfile.mkdtemp(prefix="ft_preview_"))
    lines = []
    for k, v in cfg.items():
        if k.startswith("_") or not k.isidentifier():
            continue
        lines.append(f"{k} = {v!r}")
    (workdir / "config.py").write_text("\n".join(lines) + "\n")
    return workdir


_WORKDIR = _materialise_config()
sys.path.insert(0, str(_WORKDIR))     # generated config.py wins
sys.path.insert(0, str(_HERE))        # the display/scenes/utilities stack

# overhead.py opens a SQLite DB (+ usage/override JSON) next to the module at import time.
# The preview never uses it (flights come from /api/flights), so redirect those writes to the
# throwaway workdir: keeps the preview side-effect-free AND lets it run from a read-only path
# like C:\Program Files\... (otherwise it'd try, and fail, to write the DB there).
os.environ["FT_DATA_DIR"] = str(_WORKDIR)

# ── 2. Emulator config (generated; adapter via env) + swap the LED bindings ───────
# pixel_style "real" (glowing LED dots) is supported ONLY by the pygame & browser adapters.
# tkinter/terminal/sixel support square/circle only — and worse, the emulator *crashes* (a bug
# in its own unsupported-style warning path: PixelStyle.lower()) instead of degrading, if it's
# handed a style the adapter can't do.  So pick a style the chosen adapter supports.
_REAL_OK = {"pygame", "browser"}
_pixel_style = (os.environ.get("FT_PIXEL_STYLE")
                or ("real" if ADAPTER in _REAL_OK else "circle")).lower()
if _pixel_style == "real" and ADAPTER not in _REAL_OK:
    _pixel_style = "circle"          # tkinter can't render 'real' — never feed it through
_px = 14
(_WORKDIR / "emulator_config.json").write_text(json.dumps({
    "pixel_size": _px, "pixel_style": _pixel_style, "pixel_glow": 6,
    "display_adapter": ADAPTER, "allow_adapter_fallback": True,
    "suppress_font_warnings": True, "emulator_title": "FlightTracker — live preview",
    "browser": {"port": 8888, "target_fps": 30, "quality": 80, "image_format": "JPEG"},
    "log_level": "warning",
}))
os.chdir(_WORKDIR)                     # the emulator reads emulator_config.json from CWD

import RGBMatrixEmulator                # noqa: E402
sys.modules["rgbmatrix"] = RGBMatrixEmulator
sys.modules["rgbmatrix.graphics"] = RGBMatrixEmulator.graphics
sys.modules["RPi"] = types.ModuleType("RPi")
sys.modules["RPi.GPIO"] = MagicMock()
sys.modules["RPi"].GPIO = sys.modules["RPi.GPIO"]

import logging                         # noqa: E402
for _l in ("tornado.access", "tornado.application", "tornado.general"):
    logging.getLogger(_l).setLevel(logging.ERROR)

# The pygame window adapter tries to set a PNG window icon, which fails on a pygame build
# without SDL_image ("not a Windows BMP file").  The icon is cosmetic — skip it.
if ADAPTER == "pygame":
    try:
        from RGBMatrixEmulator.adapters import pygame_adapter as _pa
        _pa.PygameAdapter._PygameAdapter__set_emulator_icon = lambda self: None
    except Exception:
        pass


# ── 3. Flights: PreviewOverhead pulls /api/flights ────────────────────────────────
# Mirrors the real utilities.overhead.Overhead contract: grab_data() runs the blocking
# HTTP GET on a short-lived daemon thread and returns instantly, so the render thread (which
# calls grab_data on a KeyFrame) NEVER blocks on I/O — the core "render thread must never
# block" rule.  _load() atomically swaps self._data and flips new_data; the render thread only
# ever reads them.  processing gates re-entry exactly like the real path so a slow/unreachable
# Pi can't pile up overlapping fetches.
class PreviewOverhead:
    def __init__(self):
        self._data = []
        self.new_data = False
        self.processing = False
        self._last_ts = None
        self._load()                      # initial synchronous load at construction (off the render thread)

    @property
    def data(self):
        self.new_data = False
        return self._data

    @property
    def data_is_empty(self):
        return len(self._data) == 0

    def _load(self):
        d = _get("/api/flights")
        if not isinstance(d, dict):
            return
        ts = d.get("ts")
        if ts != self._last_ts:
            self._last_ts = ts
            self._data = d.get("flights", []) or []   # atomic swap — render thread reads this
            self.new_data = True

    def grab_data(self):
        if self.processing:               # a previous fetch is still in flight — don't pile up
            return
        self.processing = True
        threading.Thread(target=self._grab_data, daemon=True).start()

    def _grab_data(self):
        try:
            self._load()
        finally:
            self.processing = False


import utilities.overhead as _overhead   # noqa: E402
_overhead.Overhead = PreviewOverhead


# ── 4. Weather: feed the scene the Pi's current temperature ───────────────────────
import scenes.weather as _weather         # noqa: E402

def _pi_temperature(*_a, **_k):
    d = _get("/api/weather")
    try:
        return float(d["temp"]) if d and d.get("temp") is not None else None
    except Exception:
        return None

_weather.grab_current_temperature = _pi_temperature
_weather.grab_current_temperature_openweather = lambda *a, **k: _pi_temperature()
_weather.grab_upcoming_rainfall_and_temperature = lambda *a, **k: None   # not exposed by the API


# ── 5. Scoreboard: each sport fetcher returns the Pi's active game (already chosen) ─
import scenes.sportscore as _sb           # noqa: E402
_sb_cache = {"ts": 0.0, "data": {}}

def _pi_scoreboard():
    now = time.monotonic()
    if now - _sb_cache["ts"] > 10:        # light cache — the scene polls these often
        _sb_cache["data"] = _get("/api/scoreboard", default={}) or {}
        _sb_cache["ts"] = now
    return _sb_cache["data"]

def _sport_fetcher(sport_key):
    def _f(*_a, **_k):
        sb = _pi_scoreboard()
        if sb.get("game") and sb.get("sport_key") == sport_key:
            return sb["game"]
        return None
    return _f

for _name, _key in (("_fetch_nhl", "NHL"), ("_fetch_mlb", "MLB"),
                    ("_fetch_nfl", "NFL"), ("_fetch_nba", "NBA"), ("_fetch_mls", "MLS")):
    if hasattr(_sb, _name):
        setattr(_sb, _name, _sport_fetcher(_key))


# ── View-only guarantee: never emit LAN side-effects ──────────────────────────────
# This preview SHOWS the display and nothing else — it's for watching scenes when you're not
# at the matrix.  The generated config already blanks the ding/horn hosts (so no sockets open),
# but hard-stub every LAN emitter as well, so the Mac/Windows apps can NEVER fire a plane-ding
# or goal-horn at the desktop listeners.  Those effects must follow the PHYSICAL panel only.
from utilities import planeding as _planeding   # noqa: E402
_planeding.send_ding  = lambda *a, **k: None
_planeding.send_state = lambda *a, **k: None
_sb._send_horn  = lambda *a, **k: None
_sb._send_state = lambda *a, **k: None


from display import Display              # noqa: E402  (all rgbmatrix imports → emulator)


# ── 6. Night / pause: mirror /api/status onto the flag files the Display checks ────
import display as _display               # noqa: E402
_display.PAUSE_FLAG = str(_WORKDIR / "ft_paused")
_display.NIGHT_FLAG = str(_WORKDIR / "ft_night")

def _status_poller():
    while True:
        st = _get("/api/status", default={}) or {}
        for flag, key in ((_display.PAUSE_FLAG, "paused"), (_display.NIGHT_FLAG, "night")):
            try:
                if st.get(key):
                    Path(flag).touch()
                else:
                    Path(flag).unlink(missing_ok=True)
            except Exception:
                pass
        time.sleep(3)

threading.Thread(target=_status_poller, daemon=True).start()


if __name__ == "__main__":
    print(f"[preview] pulling from {PI} · adapter={ADAPTER} · style={_pixel_style}", flush=True)
    Display().run()
