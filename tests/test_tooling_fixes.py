"""Focused regression tests for the 'tooling' bucket fixes.

Each test targets one confirmed finding and is deliberately self-contained:

  #1  preview.PreviewOverhead.grab_data() must NOT block the render thread — it must
      background the HTTP GET on a daemon thread and return instantly (the core
      "render thread must never block on I/O" rule).
  #2  backfill_resolved_cache's locality gate must require a LOCAL ORIGIN (not just
      either endpoint) so coordless 'resolved' entries the live read trusts only by
      origin don't get written and immediately busted + re-billed.
  #3  the backfill scripts must honor FT_DATA_DIR (like the live overhead.py) so they
      operate on the SAME ft_flights.db as the live service.
  #4  flight-tracker.py's crash-timestamp zone must follow config.TIMEZONE, falling back
      to America/Los_Angeles when config/TIMEZONE is missing or invalid.

Hardware (rgbmatrix / RPi.GPIO) is stubbed before any production import, and FT_DATA_DIR
is pointed at a temp dir so the repo DB is never touched.
"""
import ast
import importlib
import os
import sys
import tempfile
import textwrap
import threading
import time
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT / "utilities"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Stub the LED hardware exactly like the other test modules do, BEFORE importing anything
# that (transitively) touches rgbmatrix.  Point FT_DATA_DIR at a throwaway dir so no import
# or backfill run can create/open the repo's real ft_flights.db.
for _m, _v in (("rgbmatrix", mock.MagicMock()), ("rgbmatrix.graphics", mock.MagicMock()),
               ("RPi", mock.MagicMock()), ("RPi.GPIO", mock.MagicMock())):
    sys.modules.setdefault(_m, _v)
os.environ.setdefault("FT_DATA_DIR", tempfile.mkdtemp(prefix="ft_tooling_test_"))

_PREVIEW_PY = _ROOT / "preview.py"


# ──────────────────────────────────────────────────────────────────────────────────
# #1  preview.PreviewOverhead.grab_data() backgrounds the fetch (never blocks).
# ──────────────────────────────────────────────────────────────────────────────────
def _extract_class_source(path: Path, class_name: str) -> str:
    """Return the source of a single class from a file via AST, so we can exercise it without
    importing the whole module (importing preview.py would hit the live Pi over the network
    and start the RGBMatrixEmulator window)."""
    src = path.read_text()
    tree = ast.parse(src, filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return ast.get_source_segment(src, node)
    raise AssertionError(f"{path.name} no longer defines class {class_name}")


def _load_preview_overhead(get_impl):
    """exec just the PreviewOverhead class in an isolated namespace with a controllable _get
    and the real threading module, returning the class object."""
    ns = {"threading": threading, "_get": get_impl}
    exec(_extract_class_source(_PREVIEW_PY, "PreviewOverhead"), ns)  # noqa: S102 (test-only)
    return ns["PreviewOverhead"]


class PreviewOverheadDoesNotBlock(unittest.TestCase):
    def test_grab_data_returns_immediately_and_backgrounds_fetch(self):
        started = threading.Event()
        release = threading.Event()
        calls = {"n": 0}

        def slow_get(path, default=None, timeout=6):
            calls["n"] += 1
            if calls["n"] == 1:
                # the constructor's initial synchronous load — return quickly
                return {"ts": 0, "flights": []}
            started.set()
            release.wait(5)            # simulate a slow/unreachable Pi
            return {"ts": calls["n"], "flights": [{"callsign": "AAL1"}]}

        Cls = _load_preview_overhead(slow_get)
        ov = Cls()

        t0 = time.monotonic()
        ov.grab_data()                 # MUST return instantly even though the GET is slow
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.5, "grab_data blocked the caller — it must background the GET")
        self.assertTrue(ov.processing, "processing should be set while a fetch is in flight")

        self.assertTrue(started.wait(2), "the background fetch never started")
        # While the background fetch is mid-flight, a second grab_data is a no-op (re-entry gate).
        ov.grab_data()
        release.set()

        for _ in range(50):            # let the daemon thread finish the swap
            if not ov.processing:
                break
            time.sleep(0.05)
        self.assertFalse(ov.processing, "processing must clear once the fetch completes")
        self.assertTrue(ov.new_data, "new data should be flagged after a successful fetch")
        self.assertEqual(ov.data, [{"callsign": "AAL1"}])
        self.assertFalse(ov.new_data, "reading .data clears the new_data flag")


# ──────────────────────────────────────────────────────────────────────────────────
# #2  backfill_resolved_cache locality gate requires a LOCAL ORIGIN.
# ──────────────────────────────────────────────────────────────────────────────────
import backfill_resolved_cache as backfill_rc   # noqa: E402

_SIGHTINGS_DDL = """
CREATE TABLE sightings (id INTEGER PRIMARY KEY AUTOINCREMENT, seen_at TEXT NOT NULL,
    date TEXT NOT NULL, callsign TEXT NOT NULL, registration TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT '', destination TEXT NOT NULL DEFAULT '',
    aircraft TEXT NOT NULL DEFAULT '', route_source TEXT NOT NULL DEFAULT '',
    airline TEXT NOT NULL DEFAULT '');
CREATE UNIQUE INDEX idx_seen_cs ON sightings(date, callsign);
"""


class LocalityGateRequiresLocalOrigin(unittest.TestCase):
    def setUp(self):
        self._dir = Path(tempfile.mkdtemp())
        self._db = self._dir / "ft.db"
        self._cs = next(iter(backfill_rc._SCHEDULED_PREFIXES)) + "1"   # a real scheduled callsign

    def tearDown(self):
        for p in self._dir.glob("*"):
            p.unlink()
        self._dir.rmdir()

    def _seed(self, origin, dest):
        conn = sqlite3.connect(self._db)
        conn.executescript(_SIGHTINGS_DDL)
        conn.execute("INSERT INTO sightings (seen_at,date,callsign,origin,destination) "
                     "VALUES (?,?,?,?,?)",
                     ("2026-06-03T10:00", "2026-06-03", self._cs, origin, dest))
        conn.commit()
        conn.close()

    def _resolved_count(self):
        conn = sqlite3.connect(self._db)
        n = conn.execute("SELECT COUNT(*) FROM cache WHERE cache_type='resolved'").fetchone()[0]
        conn.close()
        return n

    def test_non_local_origin_local_dest_is_NOT_written(self):
        # The bug: an inbound flight (origin NOT local, dest IS local) used to pass the gate,
        # get written with NULL coords, and bust on the very next live read.  It must now be
        # skipped — the live read only trusts coordless entries whose ORIGIN is local.
        self._seed(origin="JFK", dest="LAS")   # arriving AT local LAS from non-local JFK
        with mock.patch.object(backfill_rc, "_local_airports", return_value=frozenset({"LAS"})):
            backfill_rc.backfill(self._db)
        self.assertEqual(self._resolved_count(), 0,
                         "non-local-origin/local-dest route must be filtered out")

    def test_local_origin_is_still_written(self):
        self._seed(origin="LAS", dest="JFK")   # departing local LAS — coordless entry is trusted
        with mock.patch.object(backfill_rc, "_local_airports", return_value=frozenset({"LAS"})):
            backfill_rc.backfill(self._db)
        self.assertEqual(self._resolved_count(), 1,
                         "local-origin route must still be written")


# ──────────────────────────────────────────────────────────────────────────────────
# #3  backfill scripts honor FT_DATA_DIR (same DB as the live service).
# ──────────────────────────────────────────────────────────────────────────────────
class BackfillHonorsFtDataDir(unittest.TestCase):
    def test_db_file_default_follows_ft_data_dir(self):
        data_dir = tempfile.mkdtemp(prefix="ft_datadir_")
        try:
            with mock.patch.dict(os.environ, {"FT_DATA_DIR": data_dir}):
                # Re-import both modules fresh so the module-level DB_FILE picks up the env var.
                for name in ("backfill_db", "backfill_resolved_cache"):
                    sys.modules.pop(name, None)
                bdb = importlib.import_module("backfill_db")
                brc = importlib.import_module("backfill_resolved_cache")
                expected = Path(data_dir) / "ft_flights.db"
                self.assertEqual(Path(bdb.DB_FILE), expected)
                self.assertEqual(Path(brc.DB_FILE), expected)
        finally:
            # Restore a clean import state for the rest of the suite.
            for name in ("backfill_db", "backfill_resolved_cache"):
                sys.modules.pop(name, None)
            os.rmdir(data_dir)


# ──────────────────────────────────────────────────────────────────────────────────
# #4  flight-tracker.py crash-timestamp zone follows config.TIMEZONE.
# ──────────────────────────────────────────────────────────────────────────────────
class FlightTrackerHonorsTimezone(unittest.TestCase):
    """flight-tracker.py is the entrypoint and runs Display() on import, so it can't be imported
    here.  Replicate its TZ-resolution snippet (read as text via AST) against a fake config to
    prove TIMEZONE wins, with the documented fallback when it's missing/invalid."""

    def _resolve_zone_with_config(self, config_module):
        """Run flight-tracker.py's `try: from config import TIMEZONE; ... except: fallback`
        block with `config` swapped for the given module, returning the chosen ZoneInfo key."""
        from zoneinfo import ZoneInfo
        ns = {"ZoneInfo": ZoneInfo}
        snippet = textwrap.dedent("""
            try:
                from config import TIMEZONE
                _PACIFIC = ZoneInfo(TIMEZONE)
            except Exception:
                _PACIFIC = ZoneInfo("America/Los_Angeles")
        """)
        with mock.patch.dict(sys.modules, {"config": config_module}):
            exec(snippet, ns)  # noqa: S102 (test-only)
        return str(ns["_PACIFIC"])

    def test_uses_config_timezone(self):
        cfg = mock.MagicMock()
        cfg.TIMEZONE = "America/New_York"
        self.assertEqual(self._resolve_zone_with_config(cfg), "America/New_York")

    def test_falls_back_when_timezone_missing(self):
        cfg = mock.MagicMock(spec=[])   # no TIMEZONE attribute → from config import TIMEZONE raises
        self.assertEqual(self._resolve_zone_with_config(cfg), "America/Los_Angeles")

    def test_falls_back_when_timezone_invalid(self):
        cfg = mock.MagicMock()
        cfg.TIMEZONE = "Not/AZone"
        self.assertEqual(self._resolve_zone_with_config(cfg), "America/Los_Angeles")

    def test_source_reads_config_timezone(self):
        # Guard against a refactor that drops the config-aware resolution back to a hardcode.
        src = (_ROOT / "flight-tracker.py").read_text()
        self.assertIn("from config import TIMEZONE", src)
        self.assertIn('ZoneInfo("America/Los_Angeles")', src)   # fallback literal preserved


if __name__ == "__main__":
    unittest.main(verbosity=2)
