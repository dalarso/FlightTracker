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
    FT_PI               base URL of the Pi web UI   (default http://192.168.1.50:5000)
    FT_PREVIEW_ADAPTER  emulator display adapter     (default 'tkinter'; 'pygame', or
                        'browser' for a local web view / headless capture)
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

PI = os.environ.get("FT_PI", "http://192.168.1.50:5000").rstrip("/")
ADAPTER = os.environ.get("FT_PREVIEW_ADAPTER", "tkinter")
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
    # Secrets are masked by the API and unused here (all data comes from the Pi) — blank
    # them so nothing tries to use the "********" sentinel as a real key.
    for k in ("OPENWEATHER_API_KEY", "OPENSKY_CLIENT_SECRET", "FLIGHTAWARE_API_KEY",
              "AIRLABS_API_KEY", "AIRLABS_API_KEY_2"):
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

# ── 2. Emulator config (generated; adapter via env) + swap the LED bindings ───────
_px = 14
(_WORKDIR / "emulator_config.json").write_text(json.dumps({
    "pixel_size": _px, "pixel_style": "real", "pixel_glow": 6,
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


# ── 3. Flights: PreviewOverhead pulls /api/flights ────────────────────────────────
class PreviewOverhead:
    def __init__(self):
        self._data = []
        self.new_data = False
        self.processing = False
        self._last_ts = None
        self._load()

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
            self._data = d.get("flights", []) or []
            self.new_data = True

    def grab_data(self):
        self._load()


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
    print(f"[preview] pulling from {PI} · adapter={ADAPTER}", flush=True)
    Display().run()
