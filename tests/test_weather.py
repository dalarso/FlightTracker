"""Tests for the weather fetch/parse helpers in scenes/weather.py.

scenes/weather.py imports rgbmatrix (LED hardware) at module top, so we stub it before
import — the WeatherScene renderer is untouched.  Only the module-level grabbers are
exercised (grab_weather, grab_current_temperature, grab_upcoming_rainfall_and_temperature,
grab_current_temperature_openweather), with the urllib HTTP boundary mocked (no network).
These parse external weather-API JSON, the same silent-break risk as the sports fetchers.
"""
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT / "scenes"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Stub the LED-matrix lib so weather.py (and the setup/ colour+font modules it pulls in)
# imports on a dev box / CI — MagicMock makes graphics.Color/Font/LoadFont no-ops.  The
# WeatherScene renderer is never instantiated here.  On the Pi the real rgbmatrix is used.
try:
    import rgbmatrix  # noqa: F401
except Exception:
    from unittest.mock import MagicMock
    _g = MagicMock(name="rgbmatrix.graphics")
    _rgb = types.ModuleType("rgbmatrix")
    _rgb.graphics = _g
    sys.modules["rgbmatrix"] = _rgb
    sys.modules["rgbmatrix.graphics"] = _g

import weather   # noqa: E402  (scenes/weather.py)


def _http(data_bytes):
    """A urlopen(...) stand-in whose .read() returns the given bytes."""
    class _R:
        def read(self):
            return data_bytes
    return _R()


def _json_http(payload):
    return _http(json.dumps(payload).encode("utf-8"))


class GrabWeather(unittest.TestCase):
    def setUp(self):
        weather.grab_weather.cache_clear()    # lru_cache — keep tests independent

    def tearDown(self):
        weather.grab_weather.cache_clear()

    def test_success_parses_json(self):
        with mock.patch("urllib.request.urlopen", return_value=_json_http({"temp_c": 21})):
            self.assertEqual(weather.grab_weather("Vegas", ttl_hash=1)["temp_c"], 21)

    def test_retries_then_succeeds(self):
        seq = [OSError("boom"), _json_http({"temp_c": 9})]
        with mock.patch("urllib.request.urlopen", side_effect=seq):
            self.assertEqual(weather.grab_weather("Vegas", ttl_hash=2)["temp_c"], 9)

    def test_all_retries_fail_raises(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            with self.assertRaises(weather.WeatherError):
                weather.grab_weather("Vegas", ttl_hash=3)


class CurrentTemperature(unittest.TestCase):
    def test_metric_passthrough(self):
        with mock.patch.object(weather, "grab_weather", return_value={"temp_c": 20}):
            self.assertEqual(weather.grab_current_temperature("Vegas", "metric"), 20)

    def test_imperial_conversion(self):
        with mock.patch.object(weather, "grab_weather", return_value={"temp_c": 20}):
            self.assertEqual(weather.grab_current_temperature("Vegas", "imperial"), 68.0)

    def test_weather_error_returns_none(self):
        with mock.patch.object(weather, "grab_weather", side_effect=weather.WeatherError("x")):
            self.assertIsNone(weather.grab_current_temperature("Vegas", "metric"))


class UpcomingRainfall(unittest.TestCase):
    @staticmethod
    def _forecast():
        # 24 hours today + 24 tomorrow = 48, so the [hour : hour+N] slice is always full.
        day = {"hourly": [{"precip_mm": h * 0.1, "temp_c": float(h), "hour": h} for h in range(24)]}
        return {"forecast": [day, day]}

    def test_returns_hours_slice_with_keys(self):
        with mock.patch.object(weather, "grab_weather", return_value=self._forecast()):
            out = weather.grab_upcoming_rainfall_and_temperature("Vegas", hours=3)
        self.assertEqual(len(out), 3)
        for item in out:
            self.assertEqual(set(item), {"precip_mm", "temp_c", "hour"})

    def test_malformed_forecast_returns_none(self):
        with mock.patch.object(weather, "grab_weather", return_value={"nope": 1}):
            self.assertIsNone(weather.grab_upcoming_rainfall_and_temperature("Vegas", 3))

    def test_weather_error_returns_none(self):
        with mock.patch.object(weather, "grab_weather", side_effect=weather.WeatherError("x")):
            self.assertIsNone(weather.grab_upcoming_rainfall_and_temperature("Vegas", 3))


class OpenWeather(unittest.TestCase):
    def test_success_extracts_temp(self):
        with mock.patch("urllib.request.urlopen", return_value=_json_http({"main": {"temp": 31.5}})):
            self.assertEqual(
                weather.grab_current_temperature_openweather("Vegas", "KEY", "metric"), 31.5)

    def test_all_retries_fail_raises(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            with self.assertRaises(weather.WeatherError):
                weather.grab_current_temperature_openweather("Vegas", "KEY", "imperial")

    def test_error_payload_missing_main_raises(self):
        # A 401-style body parses as JSON but has no "main" → KeyError → retried → WeatherError.
        with mock.patch("urllib.request.urlopen", return_value=_json_http({"cod": 401})):
            with self.assertRaises(weather.WeatherError):
                weather.grab_current_temperature_openweather("Vegas", "BADKEY", "metric")


if __name__ == "__main__":
    unittest.main(verbosity=2)
