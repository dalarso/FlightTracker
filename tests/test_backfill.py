"""Tests for the maintenance backfill scripts (utilities/backfill_db.py and
backfill_resolved_cache.py).  Pure DB/log logic — run against temp DBs + temp log/stats
files, no network, no live DB touched.  These tools rewrite the flight DB, so a net under
them guards against a silent regression in the parse / blank-fill / locality logic.
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT / "utilities"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import backfill_db                              # noqa: E402
import backfill_resolved_cache as backfill_rc   # noqa: E402

_SIGHTINGS_DDL = """
CREATE TABLE sightings (id INTEGER PRIMARY KEY AUTOINCREMENT, seen_at TEXT NOT NULL,
    date TEXT NOT NULL, callsign TEXT NOT NULL, registration TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT '', destination TEXT NOT NULL DEFAULT '',
    aircraft TEXT NOT NULL DEFAULT '', route_source TEXT NOT NULL DEFAULT '',
    airline TEXT NOT NULL DEFAULT '');
CREATE UNIQUE INDEX idx_seen_cs ON sightings(date, callsign);
"""


class BackfillDb(unittest.TestCase):
    def setUp(self):
        self._dir = Path(tempfile.mkdtemp())
        self._db = self._dir / "ft.db"

    def tearDown(self):
        for p in self._dir.glob("*"):
            p.unlink()
        self._dir.rmdir()

    def test_backfill_inserts_and_skips_test_lines(self):
        log = self._dir / "plane.log"
        log.write_text(
            "[2026-06-03 18:00:00] [route:airlabs] [type:airplanes.live] "
            "SWA123 (Southwest Airlines) LAS->JFK 'BOEING 737' N123SW\n"
            "[2026-06-03 18:05:00] [TEST:X] [route:fr24] [type:x] MXY1 LAS->SYR 'X' N0\n"
            "a noise line that should not parse as a route\n"
        )
        backfill_db.backfill(log, self._db)
        conn = sqlite3.connect(self._db)
        rows = conn.execute(
            "SELECT callsign, origin, destination, aircraft, registration, airline FROM sightings"
        ).fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)                         # [TEST: + noise skipped
        self.assertEqual(rows[0], ("SWA123", "LAS", "JFK", "BOEING 737", "N123SW", "SWA"))

    def test_backfill_fills_blanks_only(self):
        backfill_db._open_db(self._db).close()                 # create schema
        conn = sqlite3.connect(self._db)
        conn.execute("INSERT INTO sightings (seen_at,date,callsign,origin,destination,aircraft) "
                     "VALUES ('2026-06-03T18:00','2026-06-03','SWA123','LAS','','')")
        conn.commit()
        conn.close()
        log = self._dir / "plane.log"
        log.write_text("[2026-06-03 18:00:00] [route:airlabs] [type:x] SWA123 LAS->JFK 'B737' N123\n")
        backfill_db.backfill(log, self._db)
        conn = sqlite3.connect(self._db)
        row = conn.execute(
            "SELECT origin, destination, aircraft FROM sightings WHERE callsign='SWA123'"
        ).fetchone()
        conn.close()
        self.assertEqual(row, ("LAS", "JFK", "B737"))          # blanks filled, existing origin kept


class BackfillResolvedCache(unittest.TestCase):
    def setUp(self):
        self._dir = Path(tempfile.mkdtemp())
        self._db = self._dir / "ft.db"
        self._cs = next(iter(backfill_rc._SCHEDULED_PREFIXES)) + "1"   # a real scheduled callsign
        conn = sqlite3.connect(self._db)
        conn.executescript(_SIGHTINGS_DDL)
        conn.execute("INSERT INTO sightings (seen_at,date,callsign,origin,destination) VALUES (?,?,?,?,?)",
                     ("2026-06-03T10:00", "2026-06-03", self._cs, "LAS", "JFK"))
        conn.commit()
        conn.close()

    def tearDown(self):
        for p in self._dir.glob("*"):
            p.unlink()
        self._dir.rmdir()

    def test_is_scheduled(self):
        self.assertTrue(backfill_rc._is_scheduled(self._cs))
        self.assertFalse(backfill_rc._is_scheduled("N12345"))   # N-number → not scheduled
        self.assertFalse(backfill_rc._is_scheduled("AB"))       # too short

    def test_dry_run_writes_nothing(self):
        with mock.patch.object(backfill_rc, "_local_airports", return_value=frozenset({"LAS"})):
            backfill_rc.backfill(self._db, dry_run=True)
        conn = sqlite3.connect(self._db)
        n = conn.execute("SELECT COUNT(*) FROM cache WHERE cache_type='resolved'").fetchone()[0]
        conn.close()
        self.assertEqual(n, 0)

    def test_writes_resolved_for_scheduled_local_route(self):
        with mock.patch.object(backfill_rc, "_local_airports", return_value=frozenset({"LAS"})):
            backfill_rc.backfill(self._db)
        conn = sqlite3.connect(self._db)
        row = conn.execute("SELECT origin, dest, source FROM cache WHERE key=? AND cache_type='resolved'",
                           (self._cs,)).fetchone()
        conn.close()
        self.assertEqual(row, ("LAS", "JFK", "backfill"))

    def test_non_local_route_filtered_out(self):
        with mock.patch.object(backfill_rc, "_local_airports", return_value=frozenset({"PHX"})):
            backfill_rc.backfill(self._db)
        conn = sqlite3.connect(self._db)
        n = conn.execute("SELECT COUNT(*) FROM cache WHERE cache_type='resolved'").fetchone()[0]
        conn.close()
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
