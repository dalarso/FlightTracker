#!/usr/bin/env python3
"""Pixel-identical web preview of the LED display, via RGBMatrixEmulator.

This runs the EXACT same `Display` the panel runs — every scene (clock, date, day,
weather, scoreboard, flight / journey / plane cards, loading animation), the same fonts,
colours, layout and frame cadence — but with two swaps:

  1. The rpi-rgb-led-matrix C++ bindings are replaced by RGBMatrixEmulator (browser
     adapter), so the same draw calls render to a browser instead of GPIO.  Because it's
     the same draw calls, the output is pixel-identical by construction.
  2. The flight feed is sourced from /tmp/ft_data.json — the file the live display already
     publishes every poll — instead of re-polling the receiver and re-resolving routes
     (Approach B).  So the preview shows the same flights/cards as the panel, with no extra
     API usage.  Night/pause (/tmp/ft_night, /tmp/ft_paused) and the system clock are shared
     already, so those match too.

Run on the Pi alongside the real display:  env/bin/python preview.py
Then open the emulator's browser port (see emulator_config.json) or embed it in the GUI.
"""
import json
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

# ── 1. Swap the LED bindings for the emulator BEFORE anything imports `rgbmatrix` ──
import RGBMatrixEmulator
sys.modules["rgbmatrix"] = RGBMatrixEmulator
sys.modules["rgbmatrix.graphics"] = RGBMatrixEmulator.graphics

# scenes/loadingled.py drives a PHYSICAL loading LED on a GPIO pin — not part of the LED
# matrix, and the real display already owns that pin.  Stub RPi.GPIO so the preview imports
# cleanly off-Pi AND never fights the real process for the GPIO (its calls become no-ops).
sys.modules["RPi"] = types.ModuleType("RPi")
sys.modules["RPi.GPIO"] = MagicMock()
sys.modules["RPi"].GPIO = sys.modules["RPi.GPIO"]

# The emulator serves frames over a tornado server which, left alone, logs every /image
# request (~30/s) — silence its access log so the service log stays readable.
import logging  # noqa: E402
for _l in ("tornado.access", "tornado.application", "tornado.general"):
    logging.getLogger(_l).setLevel(logging.ERROR)

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
os.chdir(_HERE)          # the emulator reads emulator_config.json from the CWD

FLIGHT_DATA_FILE = "/tmp/ft_data.json"


# ── 2. Source flights from the live display's published feed (Approach B) ─────────
class PreviewOverhead:
    """Drop-in for utilities.overhead.Overhead that reads the flights the real display
    already wrote to /tmp/ft_data.json, instead of polling the receiver / re-resolving.

    Exposes exactly the interface display/__init__.py consumes: a consuming `data`
    property, `new_data`, `processing`, `data_is_empty`, and a non-blocking `grab_data()`.
    """

    def __init__(self):
        self._data = []
        self.new_data = False
        self.processing = False     # never "busy" — reads are instant
        self._last_ts = None
        self._load()                # prime so the first frame already has data

    @property
    def data(self):
        self.new_data = False       # reading consumes "new", mirroring the real Overhead
        return self._data

    @property
    def data_is_empty(self):
        return len(self._data) == 0

    def _load(self):
        try:
            d = json.loads(Path(FLIGHT_DATA_FILE).read_text())
        except Exception:
            return                  # no feed yet → keep showing the idle scenes
        ts = d.get("ts")
        if ts != self._last_ts:     # only flag new when the panel actually re-published
            self._last_ts = ts
            self._data = d.get("flights", []) or []
            self.new_data = True

    def grab_data(self):
        self._load()


# Patch the class Display imports, BEFORE importing Display (so `from utilities.overhead
# import Overhead` binds the preview version).
import utilities.overhead as _overhead          # noqa: E402
_overhead.Overhead = PreviewOverhead

from display import Display                      # noqa: E402  (all rgbmatrix imports → emulator)


if __name__ == "__main__":
    Display().run()
