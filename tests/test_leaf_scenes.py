"""Leaf-scene robustness tests (bucket: display).

JourneyScene and FlightDetailsScene render directly off the resolver-output flight
dict and used to bare-subscript it (self._data[i]["origin"], [...]["callsign"]).  A
malformed/partial dict (a future code path, an override row, a backfill entry) would
raise KeyError, which the animator swallows into a ~60 s blank scene.  The fix makes
both read via .get() with the existing blank-filler fallback; these tests pin that a
dict missing those keys degrades gracefully instead of raising.

Scenes pull in rgbmatrix transitively via setup.colours, so we stub the LED-hardware
modules before import (conftest already does this under pytest; the setdefault here
keeps the file runnable standalone).
"""
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("FT_DATA_DIR", tempfile.mkdtemp(prefix="ft-test-"))

for _m, _v in (("rgbmatrix", mock.MagicMock()), ("rgbmatrix.graphics", mock.MagicMock()),
               ("RPi", mock.MagicMock()), ("RPi.GPIO", mock.MagicMock())):
    sys.modules.setdefault(_m, _v)

from scenes.journey import JourneyScene            # noqa: E402
from scenes.flightdetails import FlightDetailsScene  # noqa: E402


def _self(data):
    s = types.SimpleNamespace()
    s._data = data
    s._data_index = 0
    s._goal_celebration_active = False
    s.canvas = mock.MagicMock()
    s.draw_square = lambda *a, **k: None
    return s


class LeafSceneRobustness(unittest.TestCase):
    def test_journey_missing_keys_does_not_raise(self):
        # No origin/destination keys at all — must fall back to the blank filler, not raise.
        JourneyScene.journey(_self([{}]))

    def test_journey_normal_dict_still_draws(self):
        JourneyScene.journey(_self([{"origin": "LAS", "destination": "SEA"}]))

    def test_journey_empty_data_bails(self):
        # The len()==0 guard must short-circuit before any indexing.
        JourneyScene.journey(_self([]))

    def test_flightdetails_missing_callsign_does_not_raise(self):
        FlightDetailsScene.flight_details(_self([{}]))

    def test_flightdetails_normal_dict_still_draws(self):
        FlightDetailsScene.flight_details(_self([{"callsign": "AAL123"}]))

    def test_flightdetails_empty_data_bails(self):
        FlightDetailsScene.flight_details(_self([]))


if __name__ == "__main__":
    unittest.main()
