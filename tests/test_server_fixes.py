"""Server-hardening tests — the fixes applied to web/server.py for the review:

  * config.py write race        → module lock + unique temp name (no shared ".tmp")
  * bool("False") coercion      → string bool values normalize correctly
  * usage-adjust temp race      → unique temp name (no shared ".tmp")
  * WEATHER_LOCATION encoding   → url-encoded at use-time (no query-param injection)
  * MAX_CONTENT_LENGTH          → oversized bodies rejected with 413 before parsing
  * SSE log-stream bound         → concurrent streams capped (503 past the cap)
  * weather/scoreboard stampede → in-flight dedup serves cached value, no duplicate fetch

Hardware-free (server.py imports Flask + utilities, no rgbmatrix), but the import is
stubbed-safe and every path that could touch the repo (CONFIG_PATH, usage files) is
redirected to a temp dir, so the live config.py / DB / usage files are never written.

    /tmp/ftvenv/bin/python -m unittest discover -s tests -p 'test_*.py'
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
_WEB = _ROOT / "web"
for _p in (str(_WEB), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_server():
    try:
        import server
        return server
    except Exception as exc:  # pragma: no cover - env-dependent
        raise unittest.SkipTest(f"server import unavailable: {exc}")


_CSRF = {"X-Requested-With": "FlightTracker"}

_REQUIRED = {
    "ZONE_HOME": {"tl_y": 36.3, "tl_x": -115.3, "br_y": 35.9, "br_x": -114.9},
    "LOCATION_HOME": [36.1, -115.1, 600.0],
}


class _CfgBase(unittest.TestCase):
    """Redirect CONFIG_PATH to a temp dir and bust the read cache around each test."""

    @classmethod
    def setUpClass(cls):
        cls.srv = _load_server()

    def setUp(self):
        self._dir = Path(tempfile.mkdtemp())
        self._cfg = self._dir / "config.py"
        self._orig = self.srv.CONFIG_PATH
        self.srv.CONFIG_PATH = self._cfg
        self._bust()

    def tearDown(self):
        self.srv.CONFIG_PATH = self._orig
        self._bust()
        for p in self._dir.glob("*"):
            p.unlink()
        self._dir.rmdir()

    def _bust(self):
        self.srv._config_cache["data"] = None
        self.srv._config_cache["mtime"] = None

    def _write(self, **extra):
        self._bust()
        data = dict(_REQUIRED)
        data.update(extra)
        self.srv.write_config(data)
        self._bust()


class BoolCoercion(_CfgBase):
    """A bool config key that arrives as the STRING 'False' must serialize to False —
    bool('False') is True, so the value would otherwise flip on save."""

    def test_string_false_stays_false(self):
        self._write(HAT_PWM_ENABLED="False")
        self.assertIs(self.srv.read_config()["HAT_PWM_ENABLED"], False)

    def test_string_zero_and_no_are_false(self):
        self._write(SCOREBOARD_NFL_ENABLED="0")
        self.assertIs(self.srv.read_config()["SCOREBOARD_NFL_ENABLED"], False)
        self._write(SCOREBOARD_NFL_ENABLED="no")
        self.assertIs(self.srv.read_config()["SCOREBOARD_NFL_ENABLED"], False)

    def test_string_true_stays_true(self):
        self._write(HAT_PWM_ENABLED="true")
        self.assertIs(self.srv.read_config()["HAT_PWM_ENABLED"], True)

    def test_real_booleans_still_round_trip(self):
        self._write(HAT_PWM_ENABLED=False, SCOREBOARD_NFL_ENABLED=True)
        cfg = self.srv.read_config()
        self.assertIs(cfg["HAT_PWM_ENABLED"], False)
        self.assertIs(cfg["SCOREBOARD_NFL_ENABLED"], True)


class ConfigWriteRace(_CfgBase):
    """write_config is serialized under a lock and uses a unique temp name, so concurrent
    saves can't clobber a shared '.tmp' or 500 on os.replace."""

    def test_no_fixed_shared_tmp_left_behind(self):
        # The old code left "<config>.tmp"; the new code uses mkstemp + cleanup.
        self._write(WEATHER_LOCATION="Las Vegas")
        self.assertFalse((self._dir / "config.py.tmp").exists())
        leftovers = [p for p in self._dir.glob("*.tmp")]
        self.assertEqual(leftovers, [])

    def test_concurrent_saves_all_succeed(self):
        # Fire many overlapping writes; with the lock + unique temp none should raise
        # FileNotFoundError on replace, and the final config must be valid & readable.
        errors = []

        def _saver(i):
            try:
                data = dict(_REQUIRED)
                data["MAX_ALTITUDE"] = 10000 + i
                self.srv.write_config(data)
            except Exception as exc:   # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=_saver, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [], f"a concurrent save failed: {errors}")
        self._bust()
        cfg = self.srv.read_config()                 # last-writer-wins, file is valid
        self.assertIn(cfg["MAX_ALTITUDE"], range(10000, 10012))
        self.assertEqual([p for p in self._dir.glob("*.tmp")], [])   # no orphaned temps


class UsageAdjustUniqueTemp(unittest.TestCase):
    """/api/usage/adjust writes via a unique temp name (not a shared '.tmp')."""

    @classmethod
    def setUpClass(cls):
        cls.srv = _load_server()
        cls.client = cls.srv.app.test_client()

    def setUp(self):
        self._dir = Path(tempfile.mkdtemp())
        self._orig = {k: getattr(self.srv, k)
                      for k in ("AIRLABS_USAGE_FILE", "AIRLABS2_USAGE_FILE", "AEROAPI_USAGE_FILE")}
        self.srv.AIRLABS_USAGE_FILE = self._dir / "al.json"
        self.srv.AIRLABS2_USAGE_FILE = self._dir / "al2.json"
        self.srv.AEROAPI_USAGE_FILE = self._dir / "fa.json"

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(self.srv, k, v)
        for p in self._dir.glob("*"):
            p.unlink()
        self._dir.rmdir()

    def test_adjust_leaves_no_fixed_tmp(self):
        r = self.client.post("/api/usage/adjust", headers=_CSRF, json={"api": "airlabs", "value": 50})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(json.loads(self.srv.AIRLABS_USAGE_FILE.read_text())["value"], 50)
        self.assertFalse((self._dir / "al.json.tmp").exists())
        self.assertEqual([p for p in self._dir.glob("*.tmp")], [])


class WeatherLocationEncoding(unittest.TestCase):
    """A WEATHER_LOCATION containing query metacharacters must be URL-encoded so it
    can't inject extra query params into the OpenWeather / taps-aff URLs."""

    @classmethod
    def setUpClass(cls):
        cls.srv = _load_server()
        cls.client = cls.srv.app.test_client()

    def setUp(self):
        # reset the in-process weather cache so each test forces a fetch
        self.srv._weather_cache.update({"temp": None, "unit": "°F", "ts": 0.0})

    def test_openweather_url_is_encoded(self):
        captured = {}

        class _Resp:
            def read(self_inner):
                return json.dumps({"main": {"temp": 70}}).encode()

        def _fake_urlopen(req, timeout=5):
            captured["url"] = req.full_url
            return _Resp()

        cfg = {"WEATHER_LOCATION": "x&appid=ATTACKER_KEY&q=", "OPENWEATHER_API_KEY": "REALKEY",
               "TEMPERATURE_UNITS": "imperial"}
        with mock.patch.object(self.srv, "read_config", return_value=cfg), \
             mock.patch.object(self.srv.urllib.request, "urlopen", _fake_urlopen):
            r = self.client.get("/api/weather")
        self.assertEqual(r.status_code, 200)
        url = captured["url"]
        # The injected appid must NOT appear as a real query param — it's percent-encoded.
        self.assertNotIn("&appid=ATTACKER_KEY", url)
        self.assertIn("appid=REALKEY", url)
        self.assertIn("ATTACKER_KEY", url)            # present, but encoded inside q=

    def test_tapsaff_path_is_encoded(self):
        captured = {}

        class _Resp:
            def read(self_inner):
                return json.dumps({"temp_c": 20}).encode()

        def _fake_urlopen(req, timeout=5):
            captured["url"] = req.full_url
            return _Resp()

        # No API key → falls through to the taps-aff path branch.
        cfg = {"WEATHER_LOCATION": "../../admin", "OPENWEATHER_API_KEY": "",
               "TEMPERATURE_UNITS": "metric"}
        with mock.patch.object(self.srv, "read_config", return_value=cfg), \
             mock.patch.object(self.srv.urllib.request, "urlopen", _fake_urlopen):
            r = self.client.get("/api/weather")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("/api/../../admin", captured["url"])   # slashes encoded, no traversal


class MaxContentLength(unittest.TestCase):
    """An oversized request body is rejected with 413 before get_json() parses it."""

    @classmethod
    def setUpClass(cls):
        cls.srv = _load_server()
        cls.client = cls.srv.app.test_client()

    def test_cap_is_set(self):
        self.assertEqual(self.srv.app.config.get("MAX_CONTENT_LENGTH"), 256 * 1024)

    def test_oversized_body_rejected(self):
        big = json.dumps({"blob": "A" * (300 * 1024)})
        r = self.client.post("/api/config", headers=_CSRF, data=big,
                             content_type="application/json")
        self.assertEqual(r.status_code, 413)


class LogStreamCap(unittest.TestCase):
    """Concurrent SSE log streams are capped; the (N+1)th gets 503 instead of pinning
    a worker. The semaphore is exercised directly so no real stream is opened."""

    @classmethod
    def setUpClass(cls):
        cls.srv = _load_server()
        cls.client = cls.srv.app.test_client()

    def test_cap_constant_and_semaphore_present(self):
        self.assertEqual(self.srv._MAX_LOG_STREAMS, 3)
        self.assertIsInstance(self.srv._LOG_STREAM_SLOTS, threading.BoundedSemaphore().__class__)

    def test_extra_stream_returns_503(self):
        # Drain all the slots, then the endpoint must refuse with 503 (not pin a worker).
        held = []
        for _ in range(self.srv._MAX_LOG_STREAMS):
            self.assertTrue(self.srv._LOG_STREAM_SLOTS.acquire(blocking=False))
            held.append(True)
        try:
            r = self.client.get("/api/log/stream")
            self.assertEqual(r.status_code, 503)
        finally:
            for _ in held:
                self.srv._LOG_STREAM_SLOTS.release()


class CacheStampedeDedup(unittest.TestCase):
    """On a cold cache, only ONE thread issues the upstream fetch; a concurrent caller
    that finds the fetch lock held serves the cached value rather than fetching again."""

    @classmethod
    def setUpClass(cls):
        cls.srv = _load_server()
        cls.client = cls.srv.app.test_client()

    def test_weather_second_caller_skips_duplicate_fetch(self):
        # Hold the in-flight fetch lock (simulating an in-progress refresh) and force a
        # cold cache; the request must return the cached value WITHOUT calling urlopen.
        self.srv._weather_cache.update({"temp": 55, "unit": "°F", "ts": 0.0})  # stale (ts=0)
        with self.srv._weather_fetch_lock:
            with mock.patch.object(self.srv.urllib.request, "urlopen",
                                   side_effect=AssertionError("must not fetch")) as m:
                r = self.client.get("/api/weather")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["temp"], 55)    # served the stale cached value
        m.assert_not_called()

    def test_scoreboard_second_caller_skips_duplicate_fetch(self):
        self.srv._scoreboard_cache.update({
            "game": None, "team_name": "VGK", "sport_key": "NHL",
            "enabled": True, "ts": 0.0, "game_ended_at": None,
        })
        with self.srv._scoreboard_fetch_lock:
            with mock.patch.object(self.srv, "_fetch_scoreboard_data",
                                   side_effect=AssertionError("must not fetch")) as m:
                r = self.client.get("/api/scoreboard")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["team_name"], "VGK")
        m.assert_not_called()


class RevealEndpoint(unittest.TestCase):
    """The /api/config/reveal GET→POST conversion: POST (not GET) so it falls under the
    CSRF guard and the key/secret never land in a URL (access logs / browser history)."""

    @classmethod
    def setUpClass(cls):
        cls.srv = _load_server()
        cls.client = cls.srv.app.test_client()
        cls.skey = next(iter(cls.srv._SENSITIVE_KEYS))   # any revealable field

    def _cfg(self, **extra):
        d = {self.skey: "s3cr3t-value"}
        d.update(extra)
        return d

    def test_reveal_rejects_get(self):
        # Converted to POST — the old log-leaking GET form is now 405.
        r = self.client.get(f"/api/config/reveal?key={self.skey}")
        self.assertEqual(r.status_code, 405)

    def test_reveal_post_returns_value_when_no_token(self):
        with mock.patch.object(self.srv, "read_config", return_value=self._cfg()):
            r = self.client.post("/api/config/reveal", json={"key": self.skey}, headers=_CSRF)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["value"], "s3cr3t-value")

    def test_reveal_rejects_unknown_key(self):
        with mock.patch.object(self.srv, "read_config", return_value=self._cfg()):
            r = self.client.post("/api/config/reveal", json={"key": "NOT_A_SECRET"}, headers=_CSRF)
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
