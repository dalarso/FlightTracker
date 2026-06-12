"""Tests for the weather-scene fixes in scenes/weather.py.

scenes/weather.py imports rgbmatrix (LED hardware) at module top, so we stub it before
import — the WeatherScene renderer is never instantiated.  We drive the methods under
test directly on a lightweight stand-in 'self' (same pattern as test_display_scroll.py),
so the real background thread never starts.  FT_DATA_DIR is pointed at a temp dir so the
repo DB is never touched on import.

Covers:
  - keep-last-good on transient temperature-fetch failure (only blank after grace window)
  - temperature_to_colour memoisation (no fresh graphics.Color per identical temp)
  - temperature_to_colour zero-width-band guard (no ZeroDivisionError)
  - clean shutdown hook stops the refresh loop
"""
import os
import sys
import types
import tempfile
import unittest
from unittest import mock
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT / "scenes"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Keep any incidental on-disk state off the real repo DB.
os.environ.setdefault("FT_DATA_DIR", tempfile.mkdtemp(prefix="ft-weather-test-"))

# Stub the LED-matrix lib so weather.py (and the setup/ colour+font modules it pulls in)
# imports on a dev box / CI.  graphics.Color(...) returns a fresh MagicMock per call, so
# distinct allocations are distinguishable by identity — exactly what the cache test needs.
try:
    import rgbmatrix  # noqa: F401
except Exception:
    from unittest.mock import MagicMock
    _g = MagicMock(name="rgbmatrix.graphics")
    _rgb = types.ModuleType("rgbmatrix")
    _rgb.graphics = _g
    sys.modules["rgbmatrix"] = _rgb
    sys.modules["rgbmatrix.graphics"] = _g

import weather  # noqa: E402  (scenes/weather.py)


def _scene_self():
    """A WeatherScene stand-in carrying only the attributes the methods read/write.

    The real __init__ spins a background thread; we bypass it and invoke the unbound
    methods on this namespace instead.
    """
    s = types.SimpleNamespace()
    s._temperature_colour_cache = {}
    s.current_temperature = None
    s.upcoming_rain_and_temp = None
    # Stand-in colour_gradient that returns a DISTINCT object per call, independent of
    # the ambient rgbmatrix.graphics.Color stub (which is bound when weather.py is first
    # imported and therefore order-dependent). This makes the memoisation assertions
    # meaningful: a cache hit returns the same object; distinct temps get distinct ones.
    s.colour_gradient = mock.Mock(side_effect=lambda a, b, r: object())
    return s


class KeepLastGoodTemperature(unittest.TestCase):
    """Finding 1: a transient fetch failure must NOT blank the last-good temperature
    until the staleness grace window has elapsed."""

    def _drive(self, provider_results, stop_after, time_seq):
        """Run _weather_refresh_loop with a scripted temperature provider and clock.

        provider_results: list of values/exceptions the single provider returns per poll
        stop_after: number of wait() calls before the stop event reports set
        time_seq:   values returned by time.time() (must outlast the loop)
        """
        s = _scene_self()

        results = iter(provider_results)

        def _provider():
            val = next(results)
            if isinstance(val, Exception):
                raise val
            return val

        s._temperature_providers = [_provider]

        waits = {"n": 0}
        stop = mock.Mock()

        def _is_set():
            return waits["n"] >= stop_after

        def _wait(_timeout):
            waits["n"] += 1

        stop.is_set.side_effect = _is_set
        stop.wait.side_effect = _wait
        s._weather_stop = stop

        with mock.patch.object(weather.time, "time", side_effect=time_seq):
            weather.WeatherScene._weather_refresh_loop(s)
        return s

    def test_transient_failure_keeps_last_good(self):
        # poll 1 succeeds (20), poll 2 fails — value must remain 20 (not None).
        # last_temp starts at 0.0, so each iteration's clock must be >= a full refresh
        # interval past the previous successful poll for the next poll to fire.
        refresh = weather.TEMPERATURE_REFRESH_SECONDS
        s = self._drive(
            provider_results=[20, weather.WeatherError("blip")],
            stop_after=2,
            time_seq=[refresh, 2 * refresh, 3 * refresh],
        )
        self.assertEqual(s.current_temperature, 20)

    def test_negative_temp_is_published(self):
        # A valid negative reading is truthy-distinct from None and must be kept.
        refresh = weather.TEMPERATURE_REFRESH_SECONDS
        s = self._drive(
            provider_results=[-5],
            stop_after=1,
            time_seq=[refresh, 2 * refresh],
        )
        self.assertEqual(s.current_temperature, -5)

    def test_blanks_after_grace_window(self):
        # First poll succeeds, then continuous failures past the staleness window blank it.
        refresh = weather.TEMPERATURE_REFRESH_SECONDS
        stale = weather.TEMPERATURE_STALE_AFTER_SECONDS
        # poll times: refresh (ok=20, failed_since=None), 2*refresh (fail -> failed_since
        # set), then a poll far enough past failed_since to exceed the grace window.
        s = self._drive(
            provider_results=[20, weather.WeatherError("x"), weather.WeatherError("x")],
            stop_after=3,
            time_seq=[refresh, 2 * refresh, 2 * refresh + stale + 1,
                      2 * refresh + stale + 2],
        )
        self.assertIsNone(s.current_temperature)


class TemperatureColourCache(unittest.TestCase):
    """Finding 2: temperature_to_colour must memoise per rounded temperature."""

    def test_same_temp_returns_cached_instance(self):
        s = _scene_self()
        c1 = weather.WeatherScene.temperature_to_colour(s, 21.0)
        c2 = weather.WeatherScene.temperature_to_colour(s, 21.4)  # rounds to same key
        self.assertIs(c1, c2)

    def test_different_temps_not_shared(self):
        s = _scene_self()
        c_cold = weather.WeatherScene.temperature_to_colour(s, -10)
        c_hot = weather.WeatherScene.temperature_to_colour(s, 35)
        self.assertIsNot(c_cold, c_hot)


class TemperatureColourZeroBandGuard(unittest.TestCase):
    """Finding 5: a zero-width band must not raise ZeroDivisionError on the render thread."""

    def test_duplicated_stop_does_not_crash(self):
        # A table with a duplicated (zero-width) band must not raise ZeroDivisionError
        # for ANY temperature — the denom guard returns ratio=1 instead of dividing.
        bad_table = (
            (0, weather.colours.WHITE),
            (10, weather.colours.BLUE_LIGHT),
            (10, weather.colours.PINK_DARK),   # duplicate threshold -> zero-width band
            (20, weather.colours.YELLOW),
        )
        with mock.patch.object(weather, "TEMPERATURE_COLOURS_METRIC", bad_table), \
             mock.patch.object(weather, "TEMPERATURE_COLOURS", bad_table):
            for temp in (-5, 0, 5, 9, 10, 10.5, 11, 15, 20, 25):
                s = _scene_self()  # fresh cache per temp so each call re-walks the table
                try:
                    colour = weather.WeatherScene.temperature_to_colour(s, temp)
                except ZeroDivisionError:
                    self.fail(f"ZeroDivisionError at temp={temp}")
                self.assertIsNotNone(colour)


class WeatherShutdownHook(unittest.TestCase):
    """Finding 6: the refresh loop exits cleanly when the stop event is set."""

    def test_stop_event_breaks_loop(self):
        s = _scene_self()
        s._temperature_providers = [lambda: 15]
        s._weather_stop = mock.Mock()
        # Loop condition is `while not stop.is_set()` — report set immediately so the loop
        # body never runs and the call returns rather than blocking forever.
        s._weather_stop.is_set.return_value = True
        with mock.patch.object(weather.time, "time", return_value=0):
            weather.WeatherScene._weather_refresh_loop(s)  # returns => loop exited cleanly
        s._weather_stop.is_set.assert_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
