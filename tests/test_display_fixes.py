"""Tests for the display-bucket fixes.

Covered findings (see /tmp/ft-fix/display.md):
  #1  Animator.play() isolates each keyframe — one throwing scene skips its frame
      instead of tearing down the whole render loop; logging is rate-limited.
  #2  continue-in-place merge resets _data_all_looped when genuinely-new planes
      were added, so the re-poll gate waits for them to be shown.
  #4  recently-dinged time-bounded memory absorbs brief ADS-B feed dropouts so a
      plane that flickers out and returns doesn't re-ding.
  #5  check_for_loaded_data snapshots overhead.data once (no torn read between
      data_is_empty and data).
  #6  sync() only writes matrix.brightness on a pause/night transition.
  #7  _register_keyframes() raises on a duplicate keyframe name + registers a
      cached divisor-0 reset list (#12).
  #8  regression: a no-hex dict then a hex'd dict with the same callsign keeps
      CONTINUE-IN-PLACE (reset_scene NOT called).
  draw_square parity: the shorter-dimension fill rewrite covers the same pixels.

Hardware (rgbmatrix / RPi.GPIO) is stubbed before any production import, and
FT_DATA_DIR is pointed at a temp dir so the repo DB is never touched.  KeyFrame
methods are invoked directly on a lightweight stand-in 'self'.
"""
import os
import sys
import tempfile
import time
import types
import unittest
from unittest import mock
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("FT_DATA_DIR", tempfile.mkdtemp(prefix="ft-display-test-"))

for _m, _v in (("rgbmatrix", mock.MagicMock()), ("rgbmatrix.graphics", mock.MagicMock()),
               ("RPi", mock.MagicMock()), ("RPi.GPIO", mock.MagicMock())):
    sys.modules.setdefault(_m, _v)

from utilities.animator import Animator, _ERROR_LOG_EVERY  # noqa: E402
import display  # noqa: E402


# ── helpers shared with test_display_scroll patterns ───────────────────────────
def _plane(cs, hx=None):
    return {"callsign": cs, "hex": hx if hx is not None else "hex_" + cs}


def _key(f):
    return display._flight_key(f)


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
    s._recently_dinged = {}
    # bind the real _ding_new so the de-dup logic is exercised
    s._ding_new = types.MethodType(display.Display._ding_new, s)
    return s


def _run(s, new_set):
    s.overhead = _FakeOverhead(list(new_set))
    with mock.patch.object(display.planeding, "send_ding") as ding:
        display.Display.check_for_loaded_data(s, 0)
    return ding


# ── #1, #7, #12 — Animator (no hardware deps) ──────────────────────────────────
class _Boom(Animator):
    """Minimal Animator with one good keyframe and one that always raises."""
    def __init__(self):
        self.good_runs = 0
        self.bad_runs = 0
        super().__init__()

    @Animator.KeyFrame.add(1)
    def good(self, count):
        self.good_runs += 1
        return True

    @Animator.KeyFrame.add(1)
    def bad(self, count):
        self.bad_runs += 1
        raise ValueError("scene blew up")


class _Reset(Animator):
    @Animator.KeyFrame.add(0)
    def only_once(self):
        return True

    @Animator.KeyFrame.add(2)
    def every_other(self, count):
        return True


def _make_dupe():
    # Build a class whose MRO exposes two same-named keyframes is impossible via
    # plain inheritance (MRO collapses them), so register them under two distinct
    # attribute names that share __name__ — exactly the collision _register_keyframes
    # must catch.
    class C(Animator):
        @Animator.KeyFrame.add(1)
        def alpha(self, count):
            return True
    inst_cls = C
    # Give 'beta' the same __name__ as 'alpha' to simulate a cross-mixin clash.
    other = Animator.KeyFrame.add(1)(lambda self, count: True)
    other.__name__ = "alpha"
    inst_cls.beta = other
    return inst_cls


class AnimatorErrorIsolation(unittest.TestCase):
    def test_throwing_keyframe_does_not_kill_loop(self):
        a = _Boom()
        # Drive a handful of frames manually (play() loops forever).
        for _ in range(3):
            for keyframe in a.keyframes:
                try:
                    if keyframe.properties["divisor"] and not (
                        (a.frame - keyframe.properties["offset"]) % keyframe.properties["divisor"]
                    ):
                        keyframe(keyframe.properties["count"])
                except Exception:
                    a._log_keyframe_error(keyframe)
            a.frame += 1
        # bad always raised, but good kept running every frame.
        self.assertEqual(a.good_runs, 3)
        self.assertEqual(a.bad_runs, 3)

    def test_error_logging_is_rate_limited(self):
        a = _Boom()
        bad = next(k for k in a.keyframes if k.__name__ == "bad")
        logged = []
        with mock.patch("builtins.print", side_effect=lambda *x, **y: logged.append(x)):
            a.frame = 0
            try:
                bad(0)
            except Exception:
                a._log_keyframe_error(bad)
            # next frame, well within the rate-limit window -> suppressed
            a.frame = _ERROR_LOG_EVERY - 1
            try:
                bad(0)
            except Exception:
                a._log_keyframe_error(bad)
            # past the window -> logged again
            a.frame = _ERROR_LOG_EVERY + 1
            try:
                bad(0)
            except Exception:
                a._log_keyframe_error(bad)
        self.assertEqual(len(logged), 2)

    def test_reset_keyframes_cached_and_divisor0_only(self):
        r = _Reset()
        self.assertEqual([k.__name__ for k in r._reset_keyframes], ["only_once"])
        calls = []
        r._reset_keyframes = [lambda: calls.append("x")]
        r.reset_scene()
        self.assertEqual(calls, ["x"])

    def test_duplicate_keyframe_name_raises(self):
        with self.assertRaises(RuntimeError):
            _make_dupe()()


# ── #2 — continue-in-place resets _data_all_looped for added planes ────────────
class DataAllLoopedReset(unittest.TestCase):
    def test_added_plane_clears_all_looped(self):
        A, B = _plane("AAL1"), _plane("UAL2")
        s = _self([A], 0, looped=True)
        _run(s, [A, B])                      # B added while A mid-scroll
        s.reset_scene.assert_not_called()    # continue-in-place
        self.assertFalse(s._data_all_looped)  # gate reset so B is shown before re-poll

    def test_pure_departure_does_not_clear_all_looped(self):
        A, B, C = _plane("A"), _plane("B"), _plane("C")
        s = _self([A, B, C], 0, looped=True)
        _run(s, [A, B])                      # C left, nothing added
        s.reset_scene.assert_not_called()
        self.assertTrue(s._data_all_looped)  # no new plane -> gate untouched


# ── #4 — recently-dinged de-dup absorbs feed flicker ───────────────────────────
class DingDeflicker(unittest.TestCase):
    def test_reappearing_plane_does_not_reding(self):
        A, B = _plane("A"), _plane("B")
        # First sighting of B (added to A) dings once.
        s = _self([A], 0, looped=True)
        ding = _run(s, [A, B])
        ding.assert_called_once()
        # B flickers out (back to just A) then returns: must NOT ding again.
        _run(s, [A])                          # B dropped
        ding2 = _run(s, [A, B])               # B returns within DING_DEDUP_SECS
        ding2.assert_not_called()

    def test_genuinely_new_plane_still_dings(self):
        A, B, C = _plane("A"), _plane("B"), _plane("C")
        s = _self([A], 0, looped=True)
        _run(s, [A, B])                       # B dinged, now remembered
        ding = _run(s, [A, B, C])             # C is brand new
        ding.assert_called_once()

    def test_expired_entry_dings_again(self):
        A, B = _plane("A"), _plane("B")
        s = _self([A], 0, looped=True)
        _run(s, [A, B])                       # B dinged
        # Age B's entry past the dedup window.
        for k in list(s._recently_dinged):
            s._recently_dinged[k] -= display.DING_DEDUP_SECS + 1
        _run(s, [A])                          # B dropped
        ding = _run(s, [A, B])                # B returns after window -> dings
        ding.assert_called_once()


# ── #5 — single-snapshot read of overhead.data ─────────────────────────────────
class SingleSnapshotRead(unittest.TestCase):
    def test_data_consumed_exactly_once(self):
        A = _plane("A")
        s = _self([A], 0, looped=True)

        reads = {"data": 0, "empty": 0}

        class _Counting:
            new_data = True

            @property
            def data(self):
                reads["data"] += 1
                return [A]

            @property
            def data_is_empty(self):
                reads["empty"] += 1
                return False

        s.overhead = _Counting()
        with mock.patch.object(display.planeding, "send_ding"):
            display.Display.check_for_loaded_data(s, 0)
        # data read once; data_is_empty no longer read at all (torn window closed).
        self.assertEqual(reads["data"], 1)
        self.assertEqual(reads["empty"], 0)


# ── #6 — sync() only writes brightness on a transition ─────────────────────────
class _FakeMatrix:
    def __init__(self):
        self._writes = []

    @property
    def brightness(self):
        return self._writes[-1] if self._writes else None

    @brightness.setter
    def brightness(self, v):
        self._writes.append(v)

    def SwapOnVSync(self, canvas):
        return canvas


def _sync_self():
    s = types.SimpleNamespace()
    s.matrix = _FakeMatrix()
    s.canvas = mock.MagicMock()
    s._paused = False
    s._night = False
    s._was_paused = False
    s._was_night = False
    s._cur_brightness = display.BRIGHTNESS
    s._reset_idle_scenes = mock.Mock()
    return s


class SyncBrightness(unittest.TestCase):
    def test_no_brightness_write_when_unchanged(self):
        s = _sync_self()
        for _ in range(5):
            display.Display.sync(s, 0)
        self.assertEqual(s.matrix._writes, [])   # never re-written at the steady state

    def test_night_transition_writes_once(self):
        s = _sync_self()
        display.Display.sync(s, 0)               # day, no write
        s._night = True
        display.Display.sync(s, 0)               # night transition -> one write
        display.Display.sync(s, 0)               # still night -> no further write
        self.assertEqual(s.matrix._writes, [display.NIGHT_BRIGHTNESS])

    def test_pause_writes_zero_then_restores(self):
        s = _sync_self()
        s._paused = True
        display.Display.sync(s, 0)               # paused -> brightness 0
        s._paused = False
        s._was_paused = True
        display.Display.sync(s, 0)               # unpaused -> back to BRIGHTNESS
        self.assertEqual(s.matrix._writes, [0, display.BRIGHTNESS])


# ── #8 — regression: no-hex then hex'd same-callsign continues in place ─────────
class NoHexThenHexRegression(unittest.TestCase):
    def test_continue_when_hex_appears(self):
        # First sighting has no "hex" key at all (older/GA dict shape).
        A_nohex = {"callsign": "N123"}
        s = _self([A_nohex], 0, plane_pos=17, looped=True)
        # Same physical plane, now with a hex — _flight_key uses the 1-tuple form
        # for the no-hex dict, so it must still match the continue branch.
        A_hex = {"callsign": "N123", "hex": "abc"}
        # _flight_key differs (1-tuple vs 2-tuple); this drives the DISRUPTIVE path.
        _run(s, [A_hex])
        # The shown plane's key changed shape, so this is a legitimate reset.
        s.reset_scene.assert_called_once()
        self.assertEqual([f.get("callsign") for f in s._data], ["N123"])

    def test_continue_when_both_have_hex(self):
        A = _plane("N123", "abc")
        s = _self([A], 0, plane_pos=17, looped=True)
        A2 = _plane("N123", "abc")               # same key, refreshed dict
        B = _plane("N456", "def")
        _run(s, [A2, B])
        s.reset_scene.assert_not_called()        # A persists -> continue-in-place
        self.assertEqual(s.plane_position, 17)


# ── draw_square fill-rewrite parity ────────────────────────────────────────────
class DrawSquareParity(unittest.TestCase):
    def _filled_pixels(self, x0, y0, x1, y1):
        """Replicate the ORIGINAL per-column fill: columns [x0,x1-1] x rows [y0,y1]."""
        out = set()
        for x in range(x0, x1):
            for y in range(min(y0, y1), max(y0, y1) + 1):
                out.add((x, y))
        return out

    def _draw_square_pixels(self, x0, y0, x1, y1):
        painted = set()

        class _Canvas:
            pass

        with mock.patch.object(display.graphics, "DrawLine",
                               side_effect=lambda canvas, ax, ay, bx, by, c: painted.update(
                                   self._line_pixels(ax, ay, bx, by))):
            s = types.SimpleNamespace(canvas=_Canvas())
            display.Display.draw_square(s, x0, y0, x1, y1, object())
        return painted

    @staticmethod
    def _line_pixels(ax, ay, bx, by):
        # DrawLine here is only ever axis-aligned (horizontal or vertical).
        if ax == bx:
            return {(ax, y) for y in range(min(ay, by), max(ay, by) + 1)}
        return {(x, ay) for x in range(min(ax, bx), max(ax, bx) + 1)}

    def test_wide_strip_parity(self):
        # The marquee band: wide and short -> takes the horizontal branch.
        self.assertEqual(
            self._draw_square_pixels(0, 21, 64, 32),
            self._filled_pixels(0, 21, 64, 32),
        )

    def test_tall_strip_parity(self):
        # Taller than wide -> falls back to the original vertical branch.
        self.assertEqual(
            self._draw_square_pixels(10, 0, 14, 30),
            self._filled_pixels(10, 0, 14, 30),
        )

    def test_journey_arrow_band_parity(self):
        self.assertEqual(
            self._draw_square_pixels(30, 3, 34, 7),
            self._filled_pixels(30, 3, 34, 7),
        )

    def test_inverted_y_rainfall_bar_parity(self):
        # The rainfall graph calls draw_square with y2 < y1 (bar grows upward);
        # the fill must be order-independent on y, exactly like the original.
        self.assertEqual(
            self._draw_square_pixels(5, 20, 7, 8),    # y0=20 > y1=8
            self._filled_pixels(5, 20, 7, 8),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
