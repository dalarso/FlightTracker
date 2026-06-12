"""Tests for the flight-rotation CONTINUE-IN-PLACE logic in
display.Display.check_for_loaded_data.

When the overhead set changes but the aircraft currently mid-scroll is still overhead,
the marquee must CONTINUE — not snap back to plane 1 and restart the scroll.  Only a
disruptive change (the plane being shown has left, or we're going back to idle) resets.

display imports rgbmatrix (LED hardware), so it's stubbed before import; the renderer is
never instantiated.  We invoke the KeyFrame method directly on a lightweight stand-in
'self', mock reset_scene/_reset_idle_scenes (so a reset is observable as a call), and mock
planeding.send_ding (so dings are observable).  reset_scene is what restarts the scroll
(via reset_scrolling), so `reset_scene.assert_not_called()` == "the scroll continued".
"""
import sys
import types
import unittest
from unittest import mock
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

for _m, _v in (("rgbmatrix", mock.MagicMock()), ("rgbmatrix.graphics", mock.MagicMock()),
               ("RPi", mock.MagicMock()), ("RPi.GPIO", mock.MagicMock())):
    sys.modules.setdefault(_m, _v)

import display  # noqa: E402


def _plane(cs, hx=None):
    return {"callsign": cs, "hex": hx if hx is not None else "hex_" + cs}


def _key(f):
    return (f["callsign"], f["hex"])


class _FakeOverhead:
    new_data = True
    def __init__(self, data):
        self._data = data
    @property
    def data(self):
        return self._data
    @property
    def data_is_empty(self):
        return len(self._data) == 0


def _self(data, idx, plane_pos=17, looped=True):
    s = types.SimpleNamespace()
    s._data = list(data)
    s._data_index = idx
    s._data_all_looped = looped
    s.plane_position = plane_pos
    s.reset_scene = mock.Mock(name="reset_scene")
    s._reset_idle_scenes = mock.Mock(name="_reset_idle_scenes")
    # Ding now routes through Display._ding_new (time-bounded dedup). Bind the real
    # method + its backing dict so the dedup path runs and reaches planeding.send_ding
    # (which _run patches). Without these the call raises AttributeError that the
    # surrounding try/except swallows, and the ding assertions silently never fire.
    s._recently_dinged = {}
    s._ding_new = types.MethodType(display.Display._ding_new, s)
    return s


def _run(s, new_set):
    s.overhead = _FakeOverhead(list(new_set))
    with mock.patch.object(display.planeding, "send_ding") as ding:
        display.Display.check_for_loaded_data(s, 0)
    return ding


class ContinueOnAddition(unittest.TestCase):
    def test_second_plane_continues_scroll(self):
        A, B = _plane("AAL1"), _plane("UAL2")
        s = _self([A], 0, plane_pos=17)
        ding = _run(s, [A, B])
        s.reset_scene.assert_not_called()                       # scroll did NOT restart
        self.assertEqual(s.plane_position, 17)                  # position untouched
        self.assertEqual(_key(s._data[s._data_index]), _key(A))  # still showing A
        self.assertIn(_key(B), {_key(f) for f in s._data})      # B now in rotation
        ding.assert_called_once()                               # dinged for the new plane

    def test_third_plane_continues_scroll(self):
        A, B, C = _plane("A"), _plane("B"), _plane("C")
        s = _self([A, B], 0, plane_pos=22)
        _run(s, [A, B, C])
        s.reset_scene.assert_not_called()
        self.assertEqual(s.plane_position, 22)
        self.assertEqual(_key(s._data[s._data_index]), _key(A))
        self.assertEqual(len(s._data), 3)

    def test_position_indicator_numerator_stable(self):
        # B shown at 2/2; adding C keeps B at index 1 -> "2/3" (numerator stable)
        A, B, C = _plane("A"), _plane("B"), _plane("C")
        s = _self([A, B], 1, plane_pos=5)
        _run(s, [A, B, C])
        self.assertEqual(s._data_index, 1)
        self.assertEqual(_key(s._data[1]), _key(B))
        s.reset_scene.assert_not_called()

    def test_new_plane_appends_after_retained(self):
        A, B = _plane("A"), _plane("B")
        s = _self([A], 0)
        _run(s, [B, A])   # overhead re-sorted B first; retained A stays first, B appends
        self.assertEqual([_key(f) for f in s._data], [_key(A), _key(B)])


class ContinueOnDeparture(unittest.TestCase):
    def test_other_plane_leaving_continues(self):
        A, B, C = _plane("A"), _plane("B"), _plane("C")
        s = _self([A, B, C], 0, plane_pos=9)
        ding = _run(s, [A, B])    # C left
        s.reset_scene.assert_not_called()
        self.assertEqual(s.plane_position, 9)
        self.assertEqual(_key(s._data[s._data_index]), _key(A))      # still showing A
        self.assertNotIn(_key(C), {_key(f) for f in s._data})        # C gone
        ding.assert_not_called()                                     # departures don't ding

    def test_earlier_plane_leaving_shifts_index_keeps_plane(self):
        # C shown at 3/3; A (before it) leaves -> C keeps scrolling, now 2/2
        A, B, C = _plane("A"), _plane("B"), _plane("C")
        s = _self([A, B, C], 2, plane_pos=14)
        _run(s, [B, C])   # A left
        s.reset_scene.assert_not_called()
        self.assertEqual(s.plane_position, 14)
        self.assertEqual(_key(s._data[s._data_index]), _key(C))      # still C
        self.assertEqual(s._data_index, 1)                           # now 2nd of 2

    def test_shown_plane_leaving_resets(self):
        A, B = _plane("A"), _plane("B")
        s = _self([A, B], 0, plane_pos=30)   # A is the one on screen
        _run(s, [B])                         # A (shown) left
        s.reset_scene.assert_called_once()   # can't keep showing a gone plane -> reset
        self.assertEqual(s._data_index, 0)
        self.assertEqual([_key(f) for f in s._data], [_key(B)])


class MixedAndDisruptive(unittest.TestCase):
    def test_add_and_remove_shown_persists_continues(self):
        A, B, C = _plane("A"), _plane("B"), _plane("C")
        s = _self([A, B], 0, plane_pos=12)   # A shown
        ding = _run(s, [A, C])               # B left, C arrived, A persists
        s.reset_scene.assert_not_called()
        self.assertEqual(_key(s._data[s._data_index]), _key(A))
        keys = {_key(f) for f in s._data}
        self.assertIn(_key(C), keys)
        self.assertNotIn(_key(B), keys)
        ding.assert_called_once()            # dinged for C

    def test_all_leave_resets_to_idle(self):
        s = _self([_plane("A")], 0)
        _run(s, [])                          # everything gone -> back to idle
        s.reset_scene.assert_called_once()
        s._reset_idle_scenes.assert_called_once()
        self.assertEqual(s._data, [])

    def test_idle_to_flights_resets(self):
        A = _plane("A")
        s = _self([], 0)                     # was idle
        _run(s, [A])
        s.reset_scene.assert_called_once()   # first plane drawn fresh
        self.assertEqual(_key(s._data[0]), _key(A))

    def test_identical_set_is_noop(self):
        A, B = _plane("A"), _plane("B")
        s = _self([A, B], 1, plane_pos=8)
        _run(s, [A, B])                      # same set
        s.reset_scene.assert_not_called()
        self.assertEqual(s._data_index, 1)   # untouched
        self.assertEqual(s.plane_position, 8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
