"""Web-layer tests — the dashboard Flask app + the three data modules extracted from
server.py (stats_data, usage_data, scoreboard_data).

server.py had no unit tests before this.  These lock in the breakup: the bind() wiring,
the pure-function correctness of each data layer, the CSRF guard, secret masking, and a
network-free GET smoke layer.  The CSRF guard is exercised directly (not via a real POST)
so the suite never triggers a live side effect — important because it also runs on the Pi.

    /tmp/ftvenv/bin/python -m unittest discover -s tests -p 'test_*.py'
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_WEB = _ROOT / "web"
for _p in (str(_WEB), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import stats_data       # noqa: E402
import usage_data       # noqa: E402
import scoreboard_data  # noqa: E402


def _load_server():
    """Import the Flask app lazily; skip app-level tests if it can't import here."""
    try:
        import server
        return server
    except Exception as exc:  # pragma: no cover - env-dependent
        raise unittest.SkipTest(f"server import unavailable: {exc}")


# ── stats_data: temp DB matching overhead's sightings/api_calls schema ──────────
_DDL = """
CREATE TABLE sightings (
    id INTEGER PRIMARY KEY AUTOINCREMENT, seen_at TEXT NOT NULL, date TEXT NOT NULL,
    callsign TEXT NOT NULL, registration TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT '', destination TEXT NOT NULL DEFAULT '',
    aircraft TEXT NOT NULL DEFAULT '', route_source TEXT NOT NULL DEFAULT '',
    airline TEXT NOT NULL DEFAULT '');
CREATE TABLE api_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, api_name TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0, UNIQUE(date, api_name));
"""
_SIGHTINGS = [
    # seen_at, date, callsign, reg, origin, dest, aircraft, route_source, airline
    ("2026-06-03T10:00:00", "2026-06-03", "SWA123", "N1SW", "LAS", "JFK", "BOEING 737", "airlabs", "SWA"),
    ("2026-06-03T10:05:00", "2026-06-03", "SWA456", "N2SW", "LAS", "MDW", "BOEING 737", "aeroapi", "SWA"),
    ("2026-06-03T10:10:00", "2026-06-03", "AAL789", "N3AA", "LAS", "DFW", "AIRBUS A321", "adsbdb", "AAL"),
    ("2026-06-02T09:00:00", "2026-06-02", "UAL111", "N4UA", "LAS", "DEN", "BOEING 757", "opensky", "UAL"),
]
_API_CALLS = [("2026-06-03", "airlabs", 5), ("2026-06-03", "aeroapi", 2), ("2026-06-02", "airlabs", 3)]


def _make_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(_DDL)
    conn.executemany(
        "INSERT INTO sightings (seen_at,date,callsign,registration,origin,destination,"
        "aircraft,route_source,airline) VALUES (?,?,?,?,?,?,?,?,?)", _SIGHTINGS)
    conn.executemany("INSERT INTO api_calls (date,api_name,count) VALUES (?,?,?)", _API_CALLS)
    conn.commit()
    conn.close()


class StatsDataLayer(unittest.TestCase):
    """The SQLite stats helpers against a seeded temp DB (the part /api/stats wraps)."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        _make_db(self._tmp.name)
        self._orig = stats_data.DB_FILE
        stats_data.DB_FILE = Path(self._tmp.name)

    def tearDown(self):
        stats_data.DB_FILE = self._orig
        try:
            os.remove(self._tmp.name)
        except OSError:
            pass

    def test_db_stats_range_aggregates(self):
        d = stats_data._db_stats("2026-06-01", "2026-06-03")
        self.assertIsNotNone(d)
        self.assertEqual(d["range_total"], 4)
        self.assertEqual(d["range_api_calls"], {"airlabs": 8, "aeroapi": 2})
        self.assertEqual(d["rollup"]["flights"], 4)
        airlines = {a["prefix"]: a["count"] for a in d["rollup"]["airlines"]}
        self.assertEqual(airlines["SWA"], 2)
        self.assertEqual(airlines["AAL"], 1)

    def test_db_stats_missing_db_returns_none(self):
        stats_data.DB_FILE = Path("/nonexistent/missing.db")
        self.assertIsNone(stats_data._db_stats("2026-06-01", "2026-06-03"))

    def test_db_recent_newest_first(self):
        rows = stats_data._db_recent("2026-06-01", "2026-06-03", limit=10)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["callsign"], "AAL789")   # 10:10 is newest
        self.assertEqual(rows[0]["airline"], "American Airlines")

    def test_db_search_by_callsign(self):
        rows, total = stats_data._db_search("SWA")
        self.assertEqual(total, 2)
        self.assertEqual({r["callsign"] for r in rows}, {"SWA123", "SWA456"})

    def test_db_search_substring_matches_origin(self):
        _, total = stats_data._db_search("LAS")   # every row departs LAS
        self.assertEqual(total, 4)

    def test_db_search_missing_db_returns_none(self):
        stats_data.DB_FILE = Path("/nonexistent/missing.db")
        self.assertIsNone(stats_data._db_search("SWA"))


class ParseLogSightings(unittest.TestCase):
    """The plane.log fallback parser (used when ft_flights.db is unavailable)."""

    def test_parses_complete_routes_and_skips_test_lines(self):
        log = (
            "[2026-06-03 18:00:00] [route:airlabs] [type:airplanes.live] "
            "SWA123 (Southwest Airlines) LAS->JFK 'BOEING 737' N123SW\n"
            "[2026-06-03 18:01:00] [route:opensky] [type:airplanes.live] "
            "AAL9 LAS->ORD 'AIRBUS A321' N9AA\n"
            "[2026-06-03 18:02:00] [TEST:MXY243] [route:fr24] [type:x] MXY243 LAS->SYR 'X' N0\n"
            "a line that should never match the route regex\n"
        )
        fh = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False)
        fh.write(log)
        fh.close()
        try:
            out = stats_data._parse_log_sightings(Path(fh.name))
        finally:
            os.remove(fh.name)
        calls = [s["callsign"] for s in out]
        self.assertIn("SWA123", calls)
        self.assertIn("AAL9", calls)
        self.assertNotIn("MXY243", calls)        # [TEST: lines are skipped
        swa = next(s for s in out if s["callsign"] == "SWA123")
        self.assertEqual((swa["origin"], swa["destination"]), ("LAS", "JFK"))
        self.assertEqual(swa["aircraft"], "BOEING 737")
        self.assertEqual(swa["registration"], "N123SW")
        self.assertEqual(swa["airline"], "Southwest Airlines")


class UsageDataLayer(unittest.TestCase):
    """Billing-period math + usage-file reset behaviour."""

    def test_period_start_is_iso_date(self):
        self.assertRegex(usage_data._billing_period_start(1), r"^\d{4}-\d{2}-\d{2}$")

    def test_period_end_after_start(self):
        # ISO date strings order lexicographically, so > is a valid chronological compare.
        self.assertGreater(usage_data._billing_period_end(15), usage_data._billing_period_start(15))

    def test_read_usage_file_period_match_kept(self):
        period = usage_data._billing_period_start(1)
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"period_start": period, "value": 42.0}, fh)
        fh.close()
        try:
            data = usage_data._read_usage_file(fh.name, 1)
        finally:
            os.remove(fh.name)
        self.assertEqual(data["value"], 42.0)
        self.assertEqual(data["period_start"], period)

    def test_read_usage_file_stale_period_resets(self):
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"period_start": "1999-01-01", "value": 999.0}, fh)
        fh.close()
        try:
            data = usage_data._read_usage_file(fh.name, 1)
        finally:
            os.remove(fh.name)
        self.assertEqual(data["value"], 0.0)

    def test_read_usage_file_missing_resets(self):
        data = usage_data._read_usage_file("/nonexistent/usage.json", 1)
        self.assertEqual(data["value"], 0.0)
        self.assertEqual(data["period_start"], usage_data._billing_period_start(1))


class ScoreboardPersistence(unittest.TestCase):
    """The post-game 'ended at' persistence (survives a web restart)."""

    def setUp(self):
        self._dir = Path(tempfile.mkdtemp())
        self._orig = scoreboard_data._GAME_ENDED_AT_FILE
        scoreboard_data._GAME_ENDED_AT_FILE = self._dir / "ended.json"

    def tearDown(self):
        scoreboard_data._GAME_ENDED_AT_FILE = self._orig
        for p in self._dir.glob("*"):
            p.unlink()
        self._dir.rmdir()

    def test_persist_load_clear_roundtrip(self):
        scoreboard_data._persist_game_ended_at("game-1", 1234.5)
        self.assertEqual(scoreboard_data._load_persisted_game_ended_at("game-1"), 1234.5)
        # A different game_id must not read another game's timestamp.
        self.assertIsNone(scoreboard_data._load_persisted_game_ended_at("game-2"))
        scoreboard_data._clear_persisted_game_ended_at()
        self.assertIsNone(scoreboard_data._load_persisted_game_ended_at("game-1"))


class SecurityAndCSRF(unittest.TestCase):
    """CSRF guard + security headers (guard tested directly — no live side effects)."""

    @classmethod
    def setUpClass(cls):
        cls.srv = _load_server()
        cls.client = cls.srv.app.test_client()

    def test_csrf_blocks_mutating_without_header(self):
        with self.srv.app.test_request_context("/api/x", method="POST"):
            resp = self.srv._csrf_guard()
        self.assertIsNotNone(resp)
        self.assertEqual(resp[1], 403)

    def test_csrf_allows_mutating_with_header(self):
        with self.srv.app.test_request_context(
                "/api/x", method="POST", headers={"X-Requested-With": "FlightTracker"}):
            self.assertIsNone(self.srv._csrf_guard())

    def test_csrf_allows_get(self):
        with self.srv.app.test_request_context("/api/x", method="GET"):
            self.assertIsNone(self.srv._csrf_guard())

    def test_security_headers_present(self):
        r = self.client.get("/api/status")
        self.assertEqual(r.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(r.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertIn("Referrer-Policy", r.headers)


class ConfigMasking(unittest.TestCase):
    """Sensitive config keys must never leave the box unmasked via /api/config."""

    @classmethod
    def setUpClass(cls):
        cls.srv = _load_server()
        cls.client = cls.srv.app.test_client()

    def test_sensitive_keys_masked(self):
        cfg = self.client.get("/api/config").get_json()
        for k in self.srv._SENSITIVE_KEYS:
            if cfg.get(k):   # set + non-empty
                self.assertEqual(cfg[k], self.srv._SECRET_SENTINEL, f"{k} leaked unmasked")


class EndpointSmoke(unittest.TestCase):
    """Network-free GET endpoints respond 200 through their (now-extracted) data layers."""

    @classmethod
    def setUpClass(cls):
        cls.client = _load_server().app.test_client()

    def test_get_endpoints_return_200(self):
        for url in ["/api/status", "/api/stats?range=today", "/api/usage",
                    "/api/stats/search?q=SWA", "/api/free-api-accuracy",
                    "/api/ga-accuracy", "/api/config", "/api/display"]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)


class ConfigWriteRoundTrip(unittest.TestCase):
    """The schema-driven write_config: round-trips every key, drops legacy keys, preserves
    unknown user keys, and never blanks a credential on a sentinel/empty save."""

    @classmethod
    def setUpClass(cls):
        cls.srv = _load_server()

    _REQUIRED = {
        "ZONE_HOME": {"tl_y": 36.3, "tl_x": -115.3, "br_y": 35.9, "br_x": -114.9},
        "LOCATION_HOME": [36.1, -115.1, 600.0],
    }

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
        data = dict(self._REQUIRED)
        data.update(extra)
        self.srv.write_config(data)
        self._bust()

    def test_roundtrip_preserves_values_and_types(self):
        self._write(WEATHER_LOCATION="Las Vegas", OPENWEATHER_API_KEY="owk-1",
                    MIN_ALTITUDE=250, GPIO_SLOWDOWN=0, HAT_PWM_ENABLED=False,
                    AIRLABS_API_KEY_2="al-2", SCOREBOARD_PRIORITY=["MLB", "NHL"],
                    SCOREBOARD_NHL_TEAM_ID=54, FEEDER_MONTHLY_CREDIT=12.5,
                    ROUTE_PAID_MISS_TTL=9999)
        cfg = self.srv.read_config()
        self.assertEqual(cfg["WEATHER_LOCATION"], "Las Vegas")
        self.assertEqual(cfg["MIN_ALTITUDE"], 250)
        self.assertEqual(cfg["GPIO_SLOWDOWN"], 0)            # 0 survives (int0 kind)
        self.assertIs(cfg["HAT_PWM_ENABLED"], False)          # False survives (bool kind)
        self.assertEqual(cfg["AIRLABS_API_KEY_2"], "al-2")    # credential not dropped
        self.assertEqual(cfg["SCOREBOARD_PRIORITY"], ["MLB", "NHL"])
        self.assertEqual(cfg["SCOREBOARD_NHL_TEAM_ID"], 54)
        self.assertEqual(cfg["FEEDER_MONTHLY_CREDIT"], 12.5)
        self.assertEqual(cfg["ROUTE_PAID_MISS_TTL"], 9999)

    def test_tricky_string_serialises_safely(self):
        self._write(WEATHER_LOCATION="x'\"y\\z")   # quotes/backslash must survive repr()
        self.assertEqual(self.srv.read_config()["WEATHER_LOCATION"], "x'\"y\\z")

    def test_legacy_key_migrated_then_dropped(self):
        self._write(SCOREBOARD_TEAM_ID=77)         # legacy key, in _KNOWN_KEYS
        cfg = self.srv.read_config()
        self.assertEqual(cfg.get("SCOREBOARD_NHL_TEAM_ID"), 77)   # migrated into the NHL slot
        self.assertNotIn("SCOREBOARD_TEAM_ID", cfg)               # not re-emitted

    def test_unknown_user_key_preserved(self):
        self._cfg.write_text('ZONE_HOME = {"tl_y": 1.0, "tl_x": 2.0, "br_y": 0.5, "br_x": 2.5}\n'
                             'LOCATION_HOME = [1.0, 2.0, 100.0]\n'
                             'MY_CUSTOM_KEY = "keep-me"\n')
        self._bust()
        self._write()                              # re-save: reads existing, preserves extras
        self.assertEqual(self.srv.read_config().get("MY_CUSTOM_KEY"), "keep-me")

    def test_sensitive_blank_keeps_existing_credential(self):
        self._write(AIRLABS_API_KEY="real-secret")
        self._write(AIRLABS_API_KEY="")                          # blank must not wipe it
        self.assertEqual(self.srv.read_config()["AIRLABS_API_KEY"], "real-secret")
        self._write(AIRLABS_API_KEY=self.srv._SECRET_SENTINEL)   # sentinel = unchanged
        self.assertEqual(self.srv.read_config()["AIRLABS_API_KEY"], "real-secret")

    def test_known_keys_derived_from_schema(self):
        derived = (set(self.srv._STRUCTURED_KEYS)
                   | {e[0] for e in self.srv._CONFIG_SCHEMA if e is not None}
                   | self.srv._LEGACY_KEYS)
        self.assertEqual(self.srv._KNOWN_KEYS, derived)


if __name__ == "__main__":
    unittest.main(verbosity=2)
