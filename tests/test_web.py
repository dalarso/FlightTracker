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
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
    """GET endpoints respond through their (now-extracted) data layers — never a 500/crash."""

    @classmethod
    def setUpClass(cls):
        cls.client = _load_server().app.test_client()

    def test_db_independent_endpoints_return_200(self):
        # These read only flags / config / usage files, so they must always serve 200.
        for url in ["/api/status", "/api/display", "/api/config", "/api/usage", "/api/scoreboard"]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_db_backed_endpoints_respond_gracefully(self):
        # 200 when the flight DB is present (the Pi), else a *handled* 503 on a DB-less
        # checkout — the point is that they never 500 / raise.
        for url in ["/api/stats?range=today", "/api/stats/search?q=SWA",
                    "/api/free-api-accuracy", "/api/ga-accuracy"]:
            with self.subTest(url=url):
                self.assertIn(self.client.get(url).status_code, (200, 503))


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

    def test_zero_preserved_for_meaningful_keys(self):
        # 0 is a real setting for these int0 keys — panel dark, display off at night, no
        # altitude floor, hide game immediately, disable goal celebration — and must
        # survive a save, not coerce to the default.
        self._write(BRIGHTNESS=0, NIGHT_BRIGHTNESS=0, MIN_ALTITUDE=0, GPIO_SLOWDOWN=0,
                    SCOREBOARD_POST_GAME_MINUTES=0, SCOREBOARD_GOAL_CELEBRATION_SECONDS=0)
        cfg = self.srv.read_config()
        self.assertEqual(cfg["BRIGHTNESS"], 0)
        self.assertEqual(cfg["NIGHT_BRIGHTNESS"], 0)
        self.assertEqual(cfg["MIN_ALTITUDE"], 0)
        self.assertEqual(cfg["GPIO_SLOWDOWN"], 0)
        self.assertEqual(cfg["SCOREBOARD_POST_GAME_MINUTES"], 0)            # int0: 0 survives
        self.assertEqual(cfg["SCOREBOARD_GOAL_CELEBRATION_SECONDS"], 0)     # int0: 0 survives

    def test_zero_feeder_credit_preserved(self):
        # A real 0.0 feeder credit (e.g. a non-feeding user) must round-trip to 0.0, not
        # silently coerce to the 10.0 default — the float0 kind preserves a meaningful 0.0.
        self._write(FEEDER_MONTHLY_CREDIT=0.0)
        self.assertEqual(self.srv.read_config()["FEEDER_MONTHLY_CREDIT"], 0.0)

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

    def test_known_keys_covers_schema_and_legacy(self):
        # Assert intent (not a re-derivation with the same expression): every schema key
        # is known, the structural + legacy + a couple of representative keys are present,
        # and no key is both a live schema key and a drop-on-save legacy key.
        schema_keys = {e[0] for e in self.srv._CONFIG_SCHEMA if e is not None}
        self.assertTrue(schema_keys <= self.srv._KNOWN_KEYS)
        for k in (*self.srv._STRUCTURED_KEYS, *self.srv._LEGACY_KEYS,
                  "AIRLABS_API_KEY", "ROUTE_PAID_MISS_TTL"):
            self.assertIn(k, self.srv._KNOWN_KEYS)
        self.assertFalse(schema_keys & self.srv._LEGACY_KEYS)


_CSRF = {"X-Requested-With": "FlightTracker"}


class ApiToggle(unittest.TestCase):
    """POST /api/apis/toggle flips per-API kill-switch flag files. All flag paths are
    redirected to a temp dir so the live display's flags are never touched."""

    @classmethod
    def setUpClass(cls):
        cls.srv = _load_server()
        cls.client = cls.srv.app.test_client()

    def setUp(self):
        self._dir = Path(tempfile.mkdtemp())
        self._orig_flags = self.srv._API_FLAGS
        self._orig_combined = self.srv.APIS_DISABLED_FLAG
        self.srv._API_FLAGS = {k: self._dir / f"ft_{k}_disabled" for k in self._orig_flags}
        self.srv.APIS_DISABLED_FLAG = self._dir / "ft_apis_disabled"

    def tearDown(self):
        self.srv._API_FLAGS = self._orig_flags
        self.srv.APIS_DISABLED_FLAG = self._orig_combined
        for p in self._dir.glob("*"):
            p.unlink()
        self._dir.rmdir()

    def test_toggle_known_api_off_then_on(self):
        flag = self.srv._API_FLAGS["adsbdb"]
        r = self.client.post("/api/apis/toggle", headers=_CSRF, json={"api": "adsbdb"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(flag.exists())                  # first toggle → disabled (flag created)
        self.assertFalse(r.get_json()["enabled"])
        r = self.client.post("/api/apis/toggle", headers=_CSRF, json={"api": "adsbdb"})
        self.assertFalse(flag.exists())                 # second toggle → enabled (flag removed)
        self.assertTrue(r.get_json()["enabled"])

    def test_unknown_api_returns_400(self):
        r = self.client.post("/api/apis/toggle", headers=_CSRF, json={"api": "bogus"})
        self.assertEqual(r.status_code, 400)

    def test_legacy_combined_switch(self):
        r = self.client.post("/api/apis/toggle", headers=_CSRF, json={})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(self.srv.APIS_DISABLED_FLAG.exists())

    def test_csrf_required(self):
        r = self.client.post("/api/apis/toggle", json={"api": "adsbdb"})   # no header
        self.assertEqual(r.status_code, 403)
        self.assertFalse(self.srv._API_FLAGS["adsbdb"].exists())           # guard ran first


class OverridesCrud(unittest.TestCase):
    """GET/POST /api/overrides round-trips override rules through SQLite. DB_FILE is
    redirected to a temp DB so the live ft_flights.db is never written."""

    _DDL = """
    CREATE TABLE overrides (id INTEGER PRIMARY KEY AUTOINCREMENT, position INTEGER NOT NULL DEFAULT 0,
        pattern TEXT NOT NULL, origin TEXT NOT NULL DEFAULT '', destination TEXT NOT NULL DEFAULT '',
        display TEXT NOT NULL DEFAULT '', plane TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '');
    CREATE TABLE overrides_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '0');
    """

    @classmethod
    def setUpClass(cls):
        cls.srv = _load_server()
        cls.client = cls.srv.app.test_client()

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        conn = sqlite3.connect(self._tmp.name)
        conn.executescript(self._DDL)
        conn.commit()
        conn.close()
        self._orig = self.srv.DB_FILE
        self.srv.DB_FILE = Path(self._tmp.name)

    def tearDown(self):
        self.srv.DB_FILE = self._orig
        os.remove(self._tmp.name)

    def test_save_then_get_roundtrip_normalises(self):
        rules = [{"pattern": "swa", "origin": "las", "destination": "jfk",
                  "display": "Southwest", "plane": "", "note": "n"}]
        r = self.client.post("/api/overrides", headers=_CSRF, json=rules)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["count"], 1)
        got = self.client.get("/api/overrides").get_json()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["pattern"], "SWA")          # pattern/codes upper-cased
        self.assertEqual(got[0]["origin"], "LAS")
        self.assertEqual(got[0]["display"], "Southwest")    # display preserved as-is

    def test_non_list_returns_400(self):
        r = self.client.post("/api/overrides", headers=_CSRF, json={"not": "a list"})
        self.assertEqual(r.status_code, 400)

    def test_rule_without_pattern_returns_400(self):
        r = self.client.post("/api/overrides", headers=_CSRF, json=[{"origin": "LAS"}])
        self.assertEqual(r.status_code, 400)

    def test_version_counter_increments(self):
        self.client.post("/api/overrides", headers=_CSRF, json=[{"pattern": "A"}])
        self.client.post("/api/overrides", headers=_CSRF, json=[{"pattern": "B"}])
        conn = sqlite3.connect(self._tmp.name)
        v = conn.execute("SELECT value FROM overrides_meta WHERE key='version'").fetchone()[0]
        conn.close()
        self.assertEqual(int(v), 2)   # bumped once per save so overhead reloads

    def test_csrf_required(self):
        r = self.client.post("/api/overrides", json=[{"pattern": "X"}])   # no header
        self.assertEqual(r.status_code, 403)


class ServiceControl(unittest.TestCase):
    """POST /api/service/{stop,start} shells out to systemctl. subprocess.run is mocked
    so the real service is NEVER touched — every test asserts the mock intercepted."""

    @classmethod
    def setUpClass(cls):
        cls.srv = _load_server()
        cls.client = cls.srv.app.test_client()

    def test_stop_invokes_systemctl(self):
        with mock.patch.object(self.srv.subprocess, "run") as m:
            r = self.client.post("/api/service/stop", headers=_CSRF)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        cmd = m.call_args[0][0]
        self.assertIn("systemctl", cmd[1])
        self.assertEqual(cmd[2:], ["stop", "FlightTracker"])

    def test_start_invokes_systemctl(self):
        with mock.patch.object(self.srv.subprocess, "run") as m:
            r = self.client.post("/api/service/start", headers=_CSRF)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(m.call_args[0][0][2:], ["start", "FlightTracker"])

    def test_timeout_returns_504(self):
        with mock.patch.object(self.srv.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("cmd", 15)):
            r = self.client.post("/api/service/stop", headers=_CSRF)
        self.assertEqual(r.status_code, 504)

    def test_called_process_error_returns_500(self):
        err = subprocess.CalledProcessError(1, "cmd", stderr=b"unit not found")
        with mock.patch.object(self.srv.subprocess, "run", side_effect=err):
            r = self.client.post("/api/service/start", headers=_CSRF)
        self.assertEqual(r.status_code, 500)

    def test_csrf_required_and_systemctl_not_called(self):
        with mock.patch.object(self.srv.subprocess, "run") as m:
            r = self.client.post("/api/service/stop")   # no header
        self.assertEqual(r.status_code, 403)
        m.assert_not_called()                            # guard short-circuits the handler


class DisplayFlags(unittest.TestCase):
    """POST /api/display/{night,off,on} flip the NIGHT_FLAG / PAUSE_FLAG files — redirected
    to a temp dir so the live display is never paused or dimmed."""

    @classmethod
    def setUpClass(cls):
        cls.srv = _load_server()
        cls.client = cls.srv.app.test_client()

    def setUp(self):
        self._dir = Path(tempfile.mkdtemp())
        self._orig = {k: getattr(self.srv, k) for k in ("NIGHT_FLAG", "PAUSE_FLAG")}
        self.srv.NIGHT_FLAG = self._dir / "ft_night"
        self.srv.PAUSE_FLAG = self._dir / "ft_paused"

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(self.srv, k, v)
        for p in self._dir.glob("*"):
            p.unlink()
        self._dir.rmdir()

    def test_night_toggles(self):
        r = self.client.post("/api/display/night", headers=_CSRF)
        self.assertTrue(r.get_json()["night"])
        self.assertTrue(self.srv.NIGHT_FLAG.exists())
        r = self.client.post("/api/display/night", headers=_CSRF)
        self.assertFalse(r.get_json()["night"])
        self.assertFalse(self.srv.NIGHT_FLAG.exists())

    def test_off_then_on(self):
        self.client.post("/api/display/off", headers=_CSRF)
        self.assertTrue(self.srv.PAUSE_FLAG.exists())
        self.client.post("/api/display/on", headers=_CSRF)
        self.assertFalse(self.srv.PAUSE_FLAG.exists())

    def test_csrf_required(self):
        self.assertEqual(self.client.post("/api/display/night").status_code, 403)
        self.assertFalse(self.srv.NIGHT_FLAG.exists())


_CACHE_DDL = """
CREATE TABLE cache (key TEXT NOT NULL, cache_type TEXT NOT NULL, origin TEXT NOT NULL DEFAULT '',
    dest TEXT NOT NULL DEFAULT '', olat REAL, olon REAL, dlat REAL, dlon REAL,
    value TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
    expires_at INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (key, cache_type));
"""


class CacheClear(unittest.TestCase):
    """POST /api/cache/clear[/entry] delete from the cache table — DB_FILE redirected to a
    temp DB so the live cache is never touched."""

    @classmethod
    def setUpClass(cls):
        cls.srv = _load_server()
        cls.client = cls.srv.app.test_client()

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        conn = sqlite3.connect(self._tmp.name)
        conn.executescript(_CACHE_DDL)
        conn.executemany(
            "INSERT INTO cache (key, cache_type, source) VALUES (?, ?, ?)",
            [("SWA1", "route", "adsbdb"), ("UAL2", "route", "opensky"), ("AAL3", "resolved", "airlabs")],
        )
        conn.commit()
        conn.close()
        self._orig = self.srv.DB_FILE
        self.srv.DB_FILE = Path(self._tmp.name)

    def tearDown(self):
        self.srv.DB_FILE = self._orig
        os.remove(self._tmp.name)

    def test_clear_by_api(self):
        r = self.client.post("/api/cache/clear", headers=_CSRF, json={"api": "adsbdb"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["deleted"], 1)          # only the adsbdb-sourced route

    def test_clear_all(self):
        r = self.client.post("/api/cache/clear", headers=_CSRF, json={"api": "all"})
        self.assertEqual(r.get_json()["deleted"], 3)          # 2 route + 1 resolved

    def test_unknown_api_returns_400(self):
        self.assertEqual(self.client.post("/api/cache/clear", headers=_CSRF,
                                          json={"api": "bogus"}).status_code, 400)

    def test_clear_entry_by_callsign(self):
        r = self.client.post("/api/cache/clear/entry", headers=_CSRF, json={"value": "SWA1"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["deleted"], 1)

    def test_clear_entry_value_required_400(self):
        self.assertEqual(self.client.post("/api/cache/clear/entry", headers=_CSRF,
                                          json={}).status_code, 400)

    def test_csrf_required(self):
        self.assertEqual(self.client.post("/api/cache/clear", json={"api": "all"}).status_code, 403)


class UsageAdjust(unittest.TestCase):
    """POST /api/usage/adjust rewrites a usage JSON file — paths redirected to a temp dir."""

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

    def test_airlabs_stored_as_int_count(self):
        r = self.client.post("/api/usage/adjust", headers=_CSRF, json={"api": "airlabs", "value": 523.7})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(json.loads(self.srv.AIRLABS_USAGE_FILE.read_text())["value"], 524)

    def test_flightaware_stored_as_dollars(self):
        r = self.client.post("/api/usage/adjust", headers=_CSRF, json={"api": "flightaware", "value": 4.2536})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(json.loads(self.srv.AEROAPI_USAGE_FILE.read_text())["value"], 4.2536)

    def test_negative_value_400(self):
        self.assertEqual(self.client.post("/api/usage/adjust", headers=_CSRF,
                                          json={"api": "airlabs", "value": -1}).status_code, 400)

    def test_unknown_api_400(self):
        self.assertEqual(self.client.post("/api/usage/adjust", headers=_CSRF,
                                          json={"api": "bogus", "value": 1}).status_code, 400)

    def test_csrf_required(self):
        self.assertEqual(self.client.post("/api/usage/adjust",
                                          json={"api": "airlabs", "value": 1}).status_code, 403)


class TestFlightEndpoint(unittest.TestCase):
    """POST /api/test_flight validates the callsign, then runs the real resolver — which we
    mock so no live API lookup fires.  (Validation rejects before the lookup, so it's safe.)"""

    @classmethod
    def setUpClass(cls):
        cls.srv = _load_server()
        cls.client = cls.srv.app.test_client()

    def test_callsign_required_400(self):
        self.assertEqual(self.client.post("/api/test_flight", headers=_CSRF, json={}).status_code, 400)

    def test_invalid_callsign_400(self):
        self.assertEqual(self.client.post("/api/test_flight", headers=_CSRF,
                                          json={"callsign": "BAD!@#"}).status_code, 400)

    def test_success_returns_lookup_result(self):
        fake = {"final_origin": "LAS", "final_destination": "JFK", "route_source": "airlabs",
                "final_plane": "B738", "type_source": "fr24"}
        with mock.patch("utilities.overhead.run_test_lookup", return_value=fake) as m:
            r = self.client.post("/api/test_flight", headers=_CSRF, json={"callsign": "SWA123"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["final_origin"], "LAS")
        m.assert_called_once()       # the real resolver was reached only via the mock

    def test_csrf_required(self):
        self.assertEqual(self.client.post("/api/test_flight",
                                          json={"callsign": "SWA123"}).status_code, 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
