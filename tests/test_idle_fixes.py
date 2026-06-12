"""Tests for the idle-scene fixes (bucket: idle).

LoadingPulseScene.loading_pulse drives the top-right corner pixel (63,0) as an
ADS-B "poll in progress" indicator while the panel is otherwise idle.  The fix
gates it on the same conditions the clock/date/day scenes use: when the
scoreboard is active OR flights are showing, the pulse must NOT paint over that
content — it blanks (63,0) and bails, so a leftover lit dot is erased and the
pulse phase restarts cleanly when idle resumes.

scenes.loadingpulse pulls in rgbmatrix only transitively via setup.colours, but
we stub the LED-hardware modules before import to match the other tests, and we
set FT_DATA_DIR to a temp dir so the repo DB is never touched.  We invoke the
KeyFrame method directly on a lightweight stand-in 'self' with a fake canvas
that records SetPixel calls.
"""
import os
import sys
import tempfile
import types
import unittest
from unittest import mock
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("FT_DATA_DIR", tempfile.mkdtemp(prefix="ft-test-"))

for _m, _v in (("rgbmatrix", mock.MagicMock()), ("rgbmatrix.graphics", mock.MagicMock()),
               ("RPi", mock.MagicMock()), ("RPi.GPIO", mock.MagicMock())):
    sys.modules.setdefault(_m, _v)


# setup.colours builds its palette from rgbmatrix.graphics.Color, and
# loading_pulse does real arithmetic (brightness * BLINKER_COLOUR.red).  Give the
# stubbed Color real numeric .red/.green/.blue so that math produces numbers, not
# MagicMocks, and our "pixel is lit" assertions are meaningful.
class _Color:
    def __init__(self, r=0, g=0, b=0):
        self.red, self.green, self.blue = r, g, b


sys.modules["rgbmatrix"].graphics.Color = _Color
sys.modules["rgbmatrix.graphics"].Color = _Color

from scenes.loadingpulse import (  # noqa: E402
    LoadingPulseScene,
    BLINKER_POSITION,
    BLINKER_STEPS,
)


class _FakeCanvas:
    def __init__(self):
        self.pixels = {}        # (x, y) -> (r, g, b)
        self.calls = []         # ordered list of SetPixel args

    def SetPixel(self, x, y, r, g, b):
        self.pixels[(x, y)] = (r, g, b)
        self.calls.append((x, y, r, g, b))


def _self(*, processing=False, scoreboard=None, data=()):
    s = types.SimpleNamespace()
    s.canvas = _FakeCanvas()
    s.overhead = types.SimpleNamespace(processing=processing)
    s._data = list(data)
    if scoreboard is not None:
        s._scoreboard_active = scoreboard
    return s


_X, _Y = BLINKER_POSITION


class LoadingPulseSuppression(unittest.TestCase):
    def test_blanks_and_bails_when_scoreboard_active(self):
        s = _self(processing=True, scoreboard=True)
        ret = LoadingPulseScene.loading_pulse(s, 0)
        # Pixel forced off, not lit
        self.assertEqual(s.canvas.pixels[(_X, _Y)], (0, 0, 0))
        self.assertEqual(s.canvas.calls, [(_X, _Y, 0, 0, 0)])
        # Returns True so count resets / pulse phase restarts cleanly
        self.assertTrue(ret)

    def test_blanks_and_bails_when_flights_showing(self):
        s = _self(processing=True, data=[{"callsign": "AAL1"}])
        ret = LoadingPulseScene.loading_pulse(s, 0)
        self.assertEqual(s.canvas.pixels[(_X, _Y)], (0, 0, 0))
        self.assertEqual(s.canvas.calls, [(_X, _Y, 0, 0, 0)])
        self.assertTrue(ret)

    def test_suppressed_even_mid_pulse_count(self):
        # A leftover lit dot from a previous frame must be erased regardless of
        # where in the pulse cycle we are.
        s = _self(processing=True, scoreboard=True)
        LoadingPulseScene.loading_pulse(s, BLINKER_STEPS // 2)
        self.assertEqual(s.canvas.pixels[(_X, _Y)], (0, 0, 0))

    def test_missing_scoreboard_attr_defaults_to_not_active(self):
        # _scoreboard_active absent (getattr default False) and no data -> pulse
        # behaves normally and lights the pixel while processing.
        s = _self(processing=True)  # no scoreboard attr, empty data
        self.assertFalse(hasattr(s, "_scoreboard_active"))
        LoadingPulseScene.loading_pulse(s, 0)
        r, g, b = s.canvas.pixels[(_X, _Y)]
        self.assertGreater(r + g + b, 0)  # pixel is lit when idle + processing


class LoadingPulseIdleBehaviourUnchanged(unittest.TestCase):
    def test_pulse_lights_pixel_when_idle_and_processing(self):
        s = _self(processing=True, scoreboard=False, data=[])
        ret = LoadingPulseScene.loading_pulse(s, 0)
        r, g, b = s.canvas.pixels[(_X, _Y)]
        self.assertGreater(r + g + b, 0)
        # count 0 -> not the final step, so it should NOT reset yet
        self.assertFalse(ret)

    def test_pulse_blanks_pixel_when_idle_and_not_processing(self):
        s = _self(processing=False, scoreboard=False, data=[])
        ret = LoadingPulseScene.loading_pulse(s, 0)
        self.assertEqual(s.canvas.pixels[(_X, _Y)], (0, 0, 0))
        self.assertTrue(ret)


if __name__ == "__main__":
    unittest.main(verbosity=2)
