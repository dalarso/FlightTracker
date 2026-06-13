"""Health surface, poll watchdog, FR24 timeout, and cache-write-failure counter
(bucket: overhead).

Covers the reliability hardening from the review:

  * FR24 lib call is bounded: _get_fr24_api sets a default socket timeout so the
    library's otherwise-untimed get_flights() can't hang a lookup thread forever.
  * Poll watchdog: _clear_if_stalled() releases a poll wedged past POLL_STALL_TIMEOUT
    so the next cycle can re-poll (and leaves a fresh poll alone).
  * Health snapshot: Overhead.health() exposes last-poll age, processing, thread
    count, and cache write failures for the /api/health endpoint.
  * Cache write failures are counted (and logged), not silently swallowed — a failing
    SD card surfaces as a rising counter instead of phantom re-resolution.
"""
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="ft_health_test_")
os.environ["FT_DATA_DIR"] = _TMP

for _m, _v in (("rgbmatrix", mock.MagicMock()), ("rgbmatrix.graphics", mock.MagicMock()),
               ("RPi", mock.MagicMock()), ("RPi.GPIO", mock.MagicMock())):
    sys.modules.setdefault(_m, _v)

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "utilities"))

import overhead                       # noqa: E402
from utilities import cache           # noqa: E402  (the module overhead binds + writes to)


class Fr24SocketTimeout(unittest.TestCase):
    def test_get_fr24_api_bounds_the_socket(self):
        with mock.patch.object(overhead, "_FR24_AVAILABLE", True), \
             mock.patch.object(overhead, "_fr24_instance", None), \
             mock.patch.object(overhead, "_FlightRadar24API", mock.MagicMock()) as _api, \
             mock.patch("socket.getdefaulttimeout", return_value=None), \
             mock.patch("socket.setdefaulttimeout") as _set:
            inst = overhead._get_fr24_api()
            _set.assert_called_once_with(overhead.FR24_HTTP_TIMEOUT)
            self.assertIs(inst, _api.return_value)

    def test_get_fr24_api_does_not_clobber_existing_timeout(self):
        with mock.patch.object(overhead, "_FR24_AVAILABLE", True), \
             mock.patch.object(overhead, "_fr24_instance", None), \
             mock.patch.object(overhead, "_FlightRadar24API", mock.MagicMock()), \
             mock.patch("socket.getdefaulttimeout", return_value=5.0), \
             mock.patch("socket.setdefaulttimeout") as _set:
            overhead._get_fr24_api()
            _set.assert_not_called()


class PollWatchdog(unittest.TestCase):
    def setUp(self):
        self.o = overhead.Overhead()

    def test_clears_a_wedged_poll(self):
        with self.o._lock:
            self.o._processing = True
            self.o._last_poll_start_ts = time.time() - (overhead.POLL_STALL_TIMEOUT + 30)
        self.assertTrue(self.o._clear_if_stalled())
        self.assertFalse(self.o.processing)

    def test_leaves_a_fresh_poll_running(self):
        with self.o._lock:
            self.o._processing = True
            self.o._last_poll_start_ts = time.time()
        self.assertFalse(self.o._clear_if_stalled())
        self.assertTrue(self.o.processing)

    def test_idle_is_not_a_stall(self):
        # Not processing → never a stall, regardless of timestamps.
        self.assertFalse(self.o._clear_if_stalled())


class HealthSnapshot(unittest.TestCase):
    def test_health_shape_before_any_poll(self):
        o = overhead.Overhead()
        h = o.health()
        self.assertEqual(
            set(h),
            {"ts", "uptime_sec", "processing", "last_poll_ok_ts",
             "last_poll_age_sec", "active_threads", "cache_write_failures"},
        )
        self.assertFalse(h["processing"])
        self.assertIsNone(h["last_poll_age_sec"])   # no successful poll yet
        self.assertEqual(h["last_poll_ok_ts"], 0)
        self.assertIsInstance(h["active_threads"], int)
        self.assertIsInstance(h["cache_write_failures"], int)

    def test_health_reflects_a_successful_poll(self):
        o = overhead.Overhead()
        with o._lock:
            o._last_poll_ok_ts = time.time()
        h = o.health()
        self.assertIsNotNone(h["last_poll_age_sec"])
        self.assertLess(h["last_poll_age_sec"], 5)

    def test_write_health_emits_a_json_file(self):
        import json
        o = overhead.Overhead()
        with mock.patch.object(overhead, "HEALTH_FILE", os.path.join(_TMP, "ft_health.json")):
            o._write_health()
            data = json.loads(Path(overhead.HEALTH_FILE).read_text())
        self.assertIn("active_threads", data)


class UsageFilePermissions(unittest.TestCase):
    """Usage files are written by the display (daemon) and read by the web UI (pi) — a
    different user — so they must be world-readable, or the API page shows 0."""

    def test_write_usage_is_world_readable(self):
        import json
        import stat
        p = os.path.join(_TMP, "airlabs_usage.json")
        overhead._write_usage(p, {"period_start": "2026-06-09", "value": 42.0})
        mode = os.stat(p).st_mode
        self.assertTrue(mode & stat.S_IROTH, "usage file must be o+r for the cross-user read")
        self.assertEqual(json.loads(Path(p).read_text())["value"], 42.0)


class CacheWriteFailureCounter(unittest.TestCase):
    def test_swallowed_write_failure_is_counted_not_silent(self):
        before = cache.write_failure_count()
        boom = mock.MagicMock()
        boom.execute.side_effect = Exception("database is locked")
        with mock.patch.object(cache, "_cache_conn", boom), \
             mock.patch.object(cache, "_cache_lock", threading.Lock()):
            # Each of the three previously-silent setters must bump the counter.
            cache._cache_db_set_aircraft("abc123", "B738", "test", 3600)
            cache._cache_db_set_reg("abc123", "N12345")
            cache._cache_db_set_paid_miss("AAL1")
        self.assertEqual(cache.write_failure_count(), before + 3)


if __name__ == "__main__":
    unittest.main()
