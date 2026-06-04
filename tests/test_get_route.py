"""
Regression tests for overhead.get_route() — Phase 0 baseline.

These drive the REAL get_route() with every external I/O point mocked, so each
test fully controls what each source "returns" and asserts the resolved
(origin, destination, source).  The point is to PIN current behavior before the
source-priority refactor, then re-run to see exactly which scenarios change.

Run:  python3 tests/test_get_route.py        (stdlib unittest, no deps)
"""
import atexit
import os
import sys
import time
import unittest
from unittest import mock

# Importing overhead initialises a SQLite DB at the repo root.  Tests mock every
# cache call, so it's never used — but remember whether one already existed so we
# can clean up our own artifact without ever deleting a real database.
_DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "ft_flights.db"))
_DB_PREEXISTED = os.path.exists(_DB_PATH)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "utilities"))
import overhead  # noqa: E402


@atexit.register
def _cleanup_test_db():
    if not _DB_PREEXISTED:
        for _ext in ("", "-wal", "-shm"):
            try:
                os.remove(_DB_PATH + _ext)
            except OSError:
                pass

# Configure the module for tests: known keys so each source is "enabled".
overhead.AIRLABS_API_KEY = "KEY1"
overhead.AIRLABS_API_KEY_2 = "KEY2"
overhead.FLIGHTAWARE_API_KEY = "FAKEY"
overhead.OPENSKY_CLIENT_ID = "cid"
overhead.OPENSKY_CLIENT_SECRET = "secret"
overhead._FR24_AVAILABLE = True


# ── Fake HTTP response ─────────────────────────────────────────────────────────
class FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


# ── Canned-response builders (simplified config → real JSON shape) ─────────────
# A source value can be:
#   None        → source returns empty 200 (no data)
#   "error"     → source returns 500 (treated as transient)
#   dict        → {"origin","dest","olat","olon","dlat","dlon"} → a route
def _route_dict(v):
    return v if isinstance(v, dict) else None


def _resp_adsbdb(v):
    r = _route_dict(v)
    if not r:
        return FakeResp(200, {"response": {"flightroute": None}})
    fr = {
        "origin": {"iata_code": r["origin"], "latitude": r.get("olat"), "longitude": r.get("olon")},
        "destination": {"iata_code": r["dest"], "latitude": r.get("dlat"), "longitude": r.get("dlon")},
    }
    return FakeResp(200, {"response": {"flightroute": fr}})


def _resp_airlabs(v):
    if isinstance(v, int):           # bare HTTP status code, e.g. 429 / 402 / 401
        return FakeResp(v, {})
    r = _route_dict(v)
    if not r:
        return FakeResp(200, {"response": {}})
    return FakeResp(200, {"response": {
        "dep_iata": r["origin"], "arr_iata": r["dest"],
        "dep_lat": r.get("olat"), "dep_lng": r.get("olon"),
        "arr_lat": r.get("dlat"), "arr_lng": r.get("dlon"),
    }})


def _resp_aeroapi(v):
    if isinstance(v, int):           # bare HTTP status code
        return FakeResp(v, {})
    r = _route_dict(v)
    if not r:
        return FakeResp(200, {"flights": []})
    return FakeResp(200, {"flights": [{
        "origin": {"code_iata": r["origin"], "latitude": r.get("olat"), "longitude": r.get("olon")},
        "destination": {"code_iata": r["dest"], "latitude": r.get("dlat"), "longitude": r.get("dlon")},
        "actual_on": None, "scheduled_out": "2026-06-01T00:00:00Z",
    }]})


def _resp_opensky(v):
    r = _route_dict(v)
    if not r:
        return FakeResp(200, [])
    # OpenSky returns ICAO codes; icao_to_iata strips K/P/C. Provide "K"+IATA.
    # Empty origin/dest → "" (partial route, e.g. departure known but arrival not).
    return FakeResp(200, [{
        "estDepartureAirport": ("K" + r["origin"]) if r.get("origin") else "",
        "estArrivalAirport":   ("K" + r["dest"]) if r.get("dest") else "",
        "firstSeen": 1000,
    }])


class FakeFlight:
    def __init__(self, origin, dest, alt=30000, ac="B738"):
        self.origin_airport_iata = origin
        self.destination_airport_iata = dest
        self.altitude = alt
        self.aircraft_code = ac


class FakeFR24:
    def __init__(self, flights):
        self._flights = flights

    def get_flights(self, registration=None, **kw):
        return list(self._flights)


def _fr24_api_for(v):
    """v: None → FR24 unavailable; [] → no flights; list of dicts → flights."""
    if v is None:
        return None
    flights = [FakeFlight(f["origin"], f["dest"], f.get("alt", 30000)) for f in v]
    return FakeFR24(flights)


# ── Scenario runner ────────────────────────────────────────────────────────────
def run_scenario(scn):
    """Install mocks for one scenario and return (origin, dest, source)."""
    calls = {"adsbdb": 0, "opensky": 0, "airlabs1": 0, "airlabs2": 0, "aeroapi": 0, "fr24": 0}

    def fake_get(url, params=None, headers=None, timeout=None, **kw):
        params = params or {}
        if "adsbdb.com/v0/callsign" in url:
            calls["adsbdb"] += 1
            return _resp_adsbdb(scn.get("adsbdb"))
        if "opensky-network.org/api/flights" in url:
            calls["opensky"] += 1
            return _resp_opensky(scn.get("opensky"))
        if "airlabs.co" in url:
            if params.get("api_key") == "KEY2":
                calls["airlabs2"] += 1
                return _resp_airlabs(scn.get("airlabs2"))
            calls["airlabs1"] += 1
            return _resp_airlabs(scn.get("airlabs1"))
        if "aeroapi.flightaware.com" in url:
            calls["aeroapi"] += 1
            return _resp_aeroapi(scn.get("aeroapi"))
        if "metadata/aircraft/icao" in url:                       # OpenSky reg-metadata
            _meta = scn.get("opensky_meta_reg")
            return FakeResp(200, {"registration": _meta}) if _meta else FakeResp(404, {})
        return FakeResp(404, {})

    def fake_cache_get_route(key, ctype):
        return scn.get("cache", {}).get((key, ctype))

    fr24_api = _fr24_api_for(scn.get("fr24", None))
    if fr24_api is not None:
        _orig_get_flights = fr24_api.get_flights

        def _counting_get_flights(*a, **kw):
            calls["fr24"] += 1
            return _orig_get_flights(*a, **kw)

        fr24_api.get_flights = _counting_get_flights

    # Stateful hex->reg cache so _try_opensky_reg's set→get roundtrip works in tests.
    _reg_store = {}
    if scn.get("hex_reg"):
        _reg_store[scn.get("hex", "abc123")] = scn["hex_reg"]

    patches = {
        "requests": mock.MagicMock(),
        "_get_fr24_api": mock.MagicMock(return_value=fr24_api),
        "_cache_db_get_route": mock.MagicMock(side_effect=fake_cache_get_route),
        "_cache_db_set_route": mock.MagicMock(),
        "_cache_db_delete_route": mock.MagicMock(),
        "_cache_db_check_paid_miss": mock.MagicMock(return_value=False),
        "_cache_db_set_paid_miss": mock.MagicMock(),
        "_cache_db_get_aircraft": mock.MagicMock(return_value=None),
        "_cache_db_set_aircraft": mock.MagicMock(),
        "_cache_db_get_reg": mock.MagicMock(side_effect=lambda hc: _reg_store.get(hc, "")),
        "_cache_db_set_reg": mock.MagicMock(side_effect=lambda hc, reg: _reg_store.__setitem__(hc, reg)),
        "_in_backoff": mock.MagicMock(side_effect=lambda api: api in scn.get("backoff", set())),
        "_match_override": mock.MagicMock(return_value=scn.get("override")),
        "_get_opensky_token": mock.MagicMock(return_value="tok"),
        "_read_usage": mock.MagicMock(side_effect=lambda path, rd: {
            "value": scn.get("usage_value", 0) if "airlabs_usage" in path else 0,
            "period_start": "2026-06-01"}),
        "_write_usage": mock.MagicMock(),
        "_airlabs_increment": mock.MagicMock(return_value=scn.get("airlabs1_count", 1)),
        "_airlabs2_increment": mock.MagicMock(return_value=scn.get("airlabs2_count", 1)),
        "_aeroapi_increment": mock.MagicMock(return_value=None),
        "_record_api_stat": mock.MagicMock(),
        "_set_backoff": mock.MagicMock(),
    }
    if scn.get("no_local"):
        patches["_LOCAL_AIRPORTS"] = frozenset()  # GLOBAL mode: no home airports
    patches["requests"].get.side_effect = fake_get

    with mock.patch.multiple(overhead, **patches), \
         mock.patch.object(overhead.os.path, "exists", return_value=False):
        plane = scn.get("plane", (None, None))
        o, d, src, _plane, _disp = overhead.get_route(
            scn.get("hex", "abc123"),
            scn["callsign"],
            0,                       # vertical_speed
            plane[0], plane[1],
            scn.get("vrs_origin", ""), scn.get("vrs_dest", ""),
            scn.get("reg", ""),
        )
        _now = int(time.time())
        cache_writes = []
        for _c in patches["_cache_db_set_route"].call_args_list:
            _a = _c.args
            _exp = _a[8] if len(_a) > 8 else _c.kwargs.get("expires_at")
            cache_writes.append({"key": _a[0], "type": _a[1],
                                 "origin": _a[2] if len(_a) > 2 else None,
                                 "dest":   _a[3] if len(_a) > 3 else None,
                                 "ttl": (_exp - _now) if _exp else None})
        backoffs = []
        for _c in patches["_set_backoff"].call_args_list:
            _api = _c.args[0] if _c.args else None
            _secs = _c.kwargs.get("secs", _c.args[1] if len(_c.args) > 1 else None)
            backoffs.append({"api": _api, "secs": _secs})
    sx = {"cache_writes": cache_writes, "backoffs": backoffs,
          "paid_miss_set": patches["_cache_db_set_paid_miss"].called}
    return o, d, src, calls, sx


# ── Baseline scenarios (CURRENT behavior) ──────────────────────────────────────
SCENARIOS = [
    {
        "name": "commercial: AirLabs-1 local wins",
        "callsign": "AAL100", "airlabs1": {"origin": "LAS", "dest": "JFK"},
        "expect": ("LAS", "JFK", "airlabs"),
        "expect_calls": {"aeroapi": 0, "airlabs2": 0, "fr24": 0},  # AirLabs resolves -> no further paid
    },
    {
        "name": "commercial: AirLabs empty -> AeroAPI local",
        "callsign": "AAL101", "airlabs1": None, "aeroapi": {"origin": "LAS", "dest": "DFW"},
        "expect": ("LAS", "DFW", "aeroapi"),
    },
    {
        "name": "GA N-number: FR24 by reg wins",
        "callsign": "N123AB", "reg": "N123AB",
        "fr24": [{"origin": "LAS", "dest": "VGT"}],
        "expect": ("LAS", "VGT", "fr24"),
    },
    {
        "name": "safety net: all paid+FR24 empty -> free consensus serves",
        "callsign": "AAL200", "adsbdb": {"origin": "LAS", "dest": "JFK"},
        "opensky": {"origin": "LAS", "dest": "JFK"},
        "airlabs1": None, "aeroapi": None,
        "expect": ("LAS", "JFK", "adsbdb+opensky"),
    },
    {
        "name": "safety net: commercial, only adsbdb has data -> adsbdb serves",
        "callsign": "AAL210", "adsbdb": {"origin": "LAS", "dest": "ORD"},
        "airlabs1": None, "aeroapi": None,
        "expect": ("LAS", "ORD", "adsbdb"),
    },
    {
        "name": "safety net: GA non-local single free source served as last resort",
        "callsign": "N210GA", "reg": "N210GA", "fr24": [],
        "adsbdb": {"origin": "DEN", "dest": "ORD"},
        "expect": ("DEN", "ORD", "adsbdb"),
    },
    {
        "name": "safety net NOT used when a paid route exists",
        "callsign": "AAL211", "adsbdb": {"origin": "LAS", "dest": "JFK"},
        "airlabs1": {"origin": "LAS", "dest": "BOS"},
        "expect": ("LAS", "BOS", "airlabs"),  # paid wins; free recorded but unused
    },
    {
        "name": "commercial: adsbdb defers, AirLabs commits",
        "callsign": "AAL201", "adsbdb": {"origin": "LAS", "dest": "JFK"},
        "airlabs1": {"origin": "LAS", "dest": "BOS"},
        "expect": ("LAS", "BOS", "airlabs"),
    },
    {
        "name": "non-local all held -> picks AirLabs (priority reorder)",
        "callsign": "AAL202", "airlabs1": {"origin": "DEN", "dest": "ORD"},
        "aeroapi": {"origin": "DEN", "dest": "ORD"},
        "expect": ("DEN", "ORD", "airlabs"),
    },
    {
        "name": "orphan FIXED: lone non-local AeroAPI now served (AirLabs empty)",
        "callsign": "AAL203", "airlabs1": None, "aeroapi": {"origin": "DEN", "dest": "ORD"},
        "expect": ("DEN", "ORD", "aeroapi"),
    },
    {
        "name": "non-local AirLabs + local AeroAPI -> AeroAPI local wins",
        "callsign": "AAL204", "airlabs1": {"origin": "DEN", "dest": "ORD"},
        "aeroapi": {"origin": "LAS", "dest": "JFK"},
        "expect": ("LAS", "JFK", "aeroapi"),
    },
    {
        "name": "override full -> immediate",
        "callsign": "AAL205",
        "override": {"pattern": "AAL205", "origin": "LAS", "destination": "MIA", "plane": "", "display": "", "note": ""},
        "expect": ("LAS", "MIA", "override"),
    },
    {
        "name": "commercial: all paid empty -> FR24 commercial fallback",
        "callsign": "AAL206", "reg": "N999XX",
        "airlabs1": None, "aeroapi": None, "fr24": [{"origin": "LAS", "dest": "SEA"}],
        "expect": ("LAS", "SEA", "fr24"),
    },
    {
        "name": "GA: FR24 empty -> adsbdb local fallback",
        "callsign": "N456CD", "reg": "N456CD", "fr24": [],
        "adsbdb": {"origin": "LAS", "dest": "HSH"},
        "expect": ("LAS", "HSH", "adsbdb"),
    },
    {
        "name": "GA consensus: adsbdb + OpenSky agree on non-local",
        "callsign": "N789EF", "reg": "N789EF", "fr24": [],
        "adsbdb": {"origin": "DEN", "dest": "ORD"}, "opensky": {"origin": "DEN", "dest": "ORD"},
        "expect": ("DEN", "ORD", "adsbdb+opensky"),
    },
    {
        # Phase 3 (_select authoritative): AeroAPI returned the COMPLETE LAS->JFK, so it
        # is credited alone — the cleaner attribution we deliberately adopted.  (Pre-flip
        # the inline logic combined AirLabs' redundant origin-only into 'airlabs+aeroapi'.)
        "name": "partial fill: AirLabs origin-only, AeroAPI returns complete -> AeroAPI credited",
        "callsign": "AAL207", "airlabs1": {"origin": "LAS", "dest": ""},
        "aeroapi": {"origin": "LAS", "dest": "JFK"},
        "expect": ("LAS", "JFK", "aeroapi"),
    },
    {
        "name": "resolved-cache hit (scheduled, local origin) -> immediate",
        "callsign": "AAL300",
        "cache": {("AAL300", "resolved"): ("LAS", "JFK", None, None, None, None, "airlabs")},
        "airlabs1": {"origin": "ZZZ", "dest": "ZZZ"},  # must NOT be consulted
        "expect": ("LAS", "JFK", "resolved:airlabs:cached"),
    },
    {
        "name": "AirLabs in backoff -> skipped, AeroAPI handles it",
        "callsign": "AAL301", "backoff": {"airlabs"},
        "airlabs1": {"origin": "LAS", "dest": "JFK"},  # has data but skipped (backoff)
        "airlabs2": None, "aeroapi": {"origin": "LAS", "dest": "DFW"},
        "expect": ("LAS", "DFW", "aeroapi"),
        "expect_calls": {"airlabs1": 0},  # backoff truly skips the live call
    },
    {
        "name": "partial override skips paid (AirLabs has dest but is skipped)",
        "callsign": "AAL302",
        "override": {"pattern": "AAL302", "origin": "LAS", "destination": "", "plane": "", "display": "", "note": ""},
        "airlabs1": {"origin": "LAS", "dest": "JFK"},  # skipped -> dest stays blank
        "expect": ("LAS", "", "override"),
    },
    {
        "name": "orphan FIXED: lone non-local FR24 now served",
        "callsign": "AAL303", "reg": "N888YY",
        "airlabs1": None, "aeroapi": None, "fr24": [{"origin": "DEN", "dest": "ORD"}],
        "expect": ("DEN", "ORD", "fr24"),
    },
    # ── geometry ON (real plane + airport coords) ──────────────────────────────
    {
        "name": "geometry: implausible AirLabs route rejected -> AeroAPI",
        "callsign": "AAL400", "plane": (36.08, -115.15),
        "airlabs1": {"origin": "JFK", "dest": "BOS",
                     "olat": 40.64, "olon": -73.78, "dlat": 42.37, "dlon": -71.01},
        "aeroapi": {"origin": "LAS", "dest": "DFW",
                    "olat": 36.08, "olon": -115.15, "dlat": 32.90, "dlon": -97.04},
        "expect": ("LAS", "DFW", "aeroapi"),
        "expect_calls": {"airlabs1": 1, "aeroapi": 1},
    },
    {
        "name": "geometry: FR24 stale leg rejected -> adsbdb local (GA)",
        "callsign": "N400GA", "reg": "N400GA", "plane": (36.08, -115.15),
        "fr24": [{"origin": "JFK", "dest": "BOS"}],   # seed table supplies coords -> implausible
        "adsbdb": {"origin": "LAS", "dest": "VGT",
                   "olat": 36.08, "olon": -115.15, "dlat": 36.21, "dlon": -115.19},
        "expect": ("LAS", "VGT", "adsbdb"),
        "expect_calls": {"fr24": 1, "adsbdb": 1},
    },
    {
        "name": "geometry: plausible route passes (control)",
        "callsign": "AAL401", "plane": (36.08, -115.15),
        "airlabs1": {"origin": "LAS", "dest": "JFK",
                     "olat": 36.08, "olon": -115.15, "dlat": 40.64, "dlon": -73.78},
        "expect": ("LAS", "JFK", "airlabs"),
        "expect_calls": {"aeroapi": 0},
    },
    # ── AirLabs-1 <-> AirLabs-2 interaction ────────────────────────────────────
    {
        "name": "AirLabs-1 backoff -> AirLabs-2 fallback wins",
        "callsign": "AAL500", "backoff": {"airlabs"},
        "airlabs1": {"origin": "LAS", "dest": "JFK"},     # skipped (backoff)
        "airlabs2": {"origin": "LAS", "dest": "BOS"}, "aeroapi": {"origin": "LAS", "dest": "DFW"},
        "expect": ("LAS", "BOS", "airlabs2"),
        "expect_calls": {"airlabs1": 0, "airlabs2": 1, "aeroapi": 0},
    },
    {
        "name": "AirLabs-1 live-empty -> AirLabs-2 SKIPPED (no double-burn)",
        "callsign": "AAL501",
        "airlabs1": None,                                  # live call returns empty
        "airlabs2": {"origin": "LAS", "dest": "BOS"},      # must NOT be consulted
        "aeroapi": {"origin": "LAS", "dest": "DFW"},
        "expect": ("LAS", "DFW", "aeroapi"),
        "expect_calls": {"airlabs1": 1, "airlabs2": 0, "aeroapi": 1},
    },
    # ── prefer-local: walk the hierarchy for a local route, even into free ──────
    {
        "name": "local-wins: free LOCAL beats trusted NON-LOCAL (hierarchy continues)",
        "callsign": "AAL600", "airlabs1": {"origin": "DEN", "dest": "ORD"},
        "adsbdb": {"origin": "LAS", "dest": "JFK"},
        "expect": ("LAS", "JFK", "adsbdb"),
    },
    {
        "name": "no local anywhere: trusted non-local beats free non-local (priority)",
        "callsign": "AAL601", "airlabs1": {"origin": "DEN", "dest": "ORD"},
        "adsbdb": {"origin": "MIA", "dest": "ATL"},
        "expect": ("DEN", "ORD", "airlabs"),
    },
    {
        "name": "trusted LOCAL still beats free LOCAL (committed inline, short-circuit)",
        "callsign": "AAL602", "aeroapi": {"origin": "LAS", "dest": "SEA"},
        "adsbdb": {"origin": "LAS", "dest": "JFK"},
        "expect": ("LAS", "SEA", "aeroapi"),
        "expect_calls": {"aeroapi": 1},
    },
    # ── GLOBAL mode: no LOCAL_AIRPORTS configured (someone else's deployment) ───
    {
        "name": "GLOBAL: AirLabs route wins and short-circuits the rest",
        "callsign": "AAL800", "no_local": True,
        "airlabs1": {"origin": "LHR", "dest": "JFK"}, "aeroapi": {"origin": "CDG", "dest": "FRA"},
        "expect": ("LHR", "JFK", "airlabs"),
        "expect_calls": {"airlabs1": 1, "aeroapi": 0},  # stops at AirLabs
    },
    {
        "name": "GLOBAL: AirLabs empty -> AeroAPI next in hierarchy",
        "callsign": "AAL801", "no_local": True,
        "airlabs1": None, "aeroapi": {"origin": "CDG", "dest": "FRA"},
        "expect": ("CDG", "FRA", "aeroapi"),
    },
    {
        "name": "GLOBAL: all trusted empty -> free fallback (still last)",
        "callsign": "AAL802", "no_local": True,
        "airlabs1": None, "aeroapi": None, "adsbdb": {"origin": "SYD", "dest": "MEL"},
        "expect": ("SYD", "MEL", "adsbdb"),
    },
    {
        "name": "partial fallback: free-only partial served instead of blank",
        "callsign": "AAL904", "airlabs1": None, "aeroapi": None,
        "adsbdb": {"origin": "LAS", "dest": ""},
        "expect": ("LAS", "", "adsbdb"),
    },
    # ── regression: bugs found in the Phase-1 adversarial review ───────────────
    {
        "name": "review-fix: implausible route NOT resurrected by last-resort picker",
        "callsign": "AAL_IMP", "plane": (36.0840, -115.1537),
        "airlabs1": {"origin": "JFK", "dest": "BOS",
                     "olat": 40.64, "olon": -73.78, "dlat": 42.37, "dlon": -71.01},
        "expect": ("", "", "none"),
    },
    {
        "name": "review-fix: VRS hint served in GLOBAL mode",
        "callsign": "AAL_VRS", "no_local": True, "vrs_origin": "SYD", "vrs_dest": "MEL",
        "airlabs1": None, "aeroapi": None,
        "expect": ("SYD", "MEL", "vrs"),
    },
    {
        "name": "review-fix: AeroAPI cached non-local route served (not dropped)",
        "callsign": "AAL_FAC", "airlabs1": None,
        "cache": {("AAL_FAC", "aeroapi"): ("DEN", "ORD", None, None, None, None, "aeroapi")},
        "expect": ("DEN", "ORD", "aeroapi:cached"),  # picker now preserves the :cached marker
    },
    {
        # Picker label honesty: a CACHED OpenSky route served as last-resort must show
        # 'opensky:cached', not a bare 'opensky' that looks live (the NV98->? confusion).
        "name": "label-fix: picker serves a cached OpenSky route as opensky:cached",
        "callsign": "AAL_LBL", "hex": "lblhex1",
        "cache": {("lblhex1", "route"): ("ABQ", "DEN", None, None, None, None, "opensky")},
        "airlabs1": None, "aeroapi": None,
        "expect": ("ABQ", "DEN", "opensky:cached"),
    },
    {
        # Non-IATA airport codes (FAA local identifiers like NV98 — the medevac case) are
        # dropped to '?' at the boundary: a 4-char code isn't a real IATA airport and
        # doesn't fit the display.  Both endpoints gone -> source "none".
        "name": "clean-iata: non-IATA code (NV98) from a source dropped to '?'",
        "callsign": "N555HE", "reg": "N555HE",
        "fr24": [{"origin": "NV98", "dest": ""}],   # FR24 returns an FAA LID, not IATA
        "expect": ("", "", "none"),
    },
    # ── decision B: a local endpoint outranks completeness ─────────────────────
    {
        "name": "decision-B: local partial beats held non-local complete",
        "callsign": "AAL_LPB", "airlabs1": {"origin": "LAS", "dest": ""},
        "aeroapi": {"origin": "DEN", "dest": "ORD"},
        "expect": ("LAS", "", "airlabs"),
    },
    {
        "name": "decision-B: local complete still upgrades a local partial",
        "callsign": "AAL_LPC", "airlabs1": {"origin": "LAS", "dest": ""},
        "adsbdb": {"origin": "LAS", "dest": "JFK"},
        "expect": ("LAS", "JFK", "adsbdb"),
    },
    # ── side-effect pins: cache TTL + backoff (protect the AirLabs refactor) ────
    {
        "name": "ttl: AirLabs local route cached at ROUTE_TTL_DEFAULT",
        "callsign": "AAL_T1", "airlabs1": {"origin": "LAS", "dest": "JFK"},
        "expect": ("LAS", "JFK", "airlabs"),
        "expect_cache_ttl": {"airlabs:AAL_T1": overhead.ROUTE_TTL_DEFAULT},
    },
    {
        "name": "ttl: AirLabs non-local route cached at ROUTE_MISS_TTL",
        "callsign": "AAL_T2", "airlabs1": {"origin": "DEN", "dest": "ORD"}, "aeroapi": None,
        "expect": ("DEN", "ORD", "airlabs"),
        "expect_cache_ttl": {"airlabs:AAL_T2": overhead.ROUTE_MISS_TTL},
    },
    {
        "name": "backoff: AirLabs 429 -> 1h rate-limit backoff",
        "callsign": "AAL_T3", "airlabs1": 429, "aeroapi": None,
        "expect": ("", "", "none"),
        "expect_backoff": [("airlabs", overhead.BACKOFF_RATE_LIMIT_SECS)],
    },
    {
        "name": "backoff: AirLabs 402 -> 24h quota-probe backoff",
        "callsign": "AAL_T4", "airlabs1": 402, "aeroapi": None,
        "expect": ("", "", "none"),
        "expect_backoff": [("airlabs", overhead.QUOTA_PROBE_BACKOFF_SECS)],
    },
    {
        "name": "backoff: AeroAPI 402 -> 24h quota-probe backoff (over budget)",
        "callsign": "AAL_FA2", "airlabs1": None, "airlabs2": None, "aeroapi": 402,
        "expect": ("", "", "none"),
        "expect_backoff": [("aeroapi", overhead.QUOTA_PROBE_BACKOFF_SECS)],
    },
    {
        "name": "backoff: AirLabs-2 429 (AL-1 in backoff) -> airlabs2 backoff",
        "callsign": "AAL_T5", "backoff": {"airlabs"}, "airlabs2": 429, "aeroapi": None,
        "expect": ("", "", "none"),
        "expect_backoff": [("airlabs2", overhead.BACKOFF_RATE_LIMIT_SECS)],
    },
    # ── regression (MXY243): a LOCAL partial must beat a stale non-local complete ──
    {
        "name": "MXY243: OpenSky local partial beats stale non-local complete",
        "callsign": "MXY243", "plane": (36.0840, -115.1537),     # over LAS
        "backoff": {"airlabs"},                                   # AirLabs-1 in backoff
        "opensky": {"origin": "LAS", "dest": ""},                 # correct local origin
        "adsbdb":  {"origin": "SYR", "dest": "CHS",               # stale prior leg, non-local
                    "olat": 43.11, "olon": -76.11, "dlat": 32.90, "dlon": -80.04},
        "airlabs2": {"origin": "SYR", "dest": "CHS",
                     "olat": 43.11, "olon": -76.11, "dlat": 32.90, "dlon": -80.04},
        "aeroapi":  {"origin": "SYR", "dest": "CHS",
                     "olat": 43.11, "olon": -76.11, "dlat": 32.90, "dlon": -80.04},
        "expect": ("LAS", "", "opensky"),
    },
    {
        "name": "MXY243 (no coords): tier alone — local partial beats non-local complete",
        "callsign": "MXY244", "opensky": {"origin": "LAS", "dest": ""},
        "adsbdb": {"origin": "SYR", "dest": "CHS"}, "airlabs1": None,
        "aeroapi": {"origin": "SYR", "dest": "CHS"},
        "expect": ("LAS", "", "opensky"),
    },
    {
        "name": "commercial FR24 runs via cached hex->reg (feed lacks tail)",
        "callsign": "AAL_FRX", "hex_reg": "N218BZ",          # tail from hex cache, NOT feed
        "backoff": {"airlabs"},
        "adsbdb": {"origin": "SYR", "dest": "CHS"},
        "airlabs2": {"origin": "SYR", "dest": "CHS"},
        "aeroapi": {"origin": "SYR", "dest": "CHS"},
        "fr24": [{"origin": "LAS", "dest": "SYR"}],           # FR24 has the correct current leg
        "expect": ("LAS", "SYR", "fr24"),
    },
    # ── review round 2: efficiency + robustness fixes ──────────────────────────
    {
        # FR24 is FREE — its having a route must NOT keep the (empty) paid APIs from
        # being suppressed for 2 h, or they re-bill every poll while the plane lingers.
        "name": "review2: paid-miss recorded even when FR24 (free) resolved the route",
        "callsign": "AAL_PM", "hex_reg": "N111AA",            # commercial; tail via hex cache
        "airlabs1": None, "aeroapi": None,                    # paid chain returns nothing
        "fr24": [{"origin": "LAS", "dest": "SYR"}],           # free FR24 supplies the route
        "expect": ("LAS", "SYR", "fr24"),
        "expect_paid_miss": True,
    },
    {
        # An implausible AeroAPI route must end up negatively cached, not deleted —
        # otherwise the next poll busts+refetches it and re-bills AeroAPI every ~15 s.
        "name": "review2: implausible AeroAPI route negatively cached (no refetch loop)",
        "callsign": "AAL_NEG", "plane": (36.0840, -115.1537),  # over LAS
        "airlabs1": None,
        "aeroapi": {"origin": "SYR", "dest": "CHS",            # stale leg, far from LAS
                    "olat": 43.11, "olon": -76.11, "dlat": 32.90, "dlon": -80.04},
        "expect": ("", "", "none"),
        "expect_cache_empty": ["AAL_NEG"],                    # last aeroapi write is negative
    },
    {
        # GLOBAL mode has no "home", so a reused flight number must NOT persist for
        # 7 days in the resolved cache — it would serve a stale route for days.
        "name": "review2: GLOBAL mode never writes the 7-day resolved cache",
        "callsign": "AAL_GBL", "no_local": True, "airlabs1": None,
        "aeroapi": {"origin": "LHR", "dest": "JFK",
                    "olat": 51.47, "olon": -0.45, "dlat": 40.64, "dlon": -73.78},
        "expect": ("LHR", "JFK", "aeroapi"),
        "expect_no_cache_types": ["resolved"],
    },
    {
        # Non-local complete OpenSky route gets the 5-min floor (was 1 h) so a stale
        # hex-keyed entry can't linger — parity with the paid sources' TTL policy.
        "name": "review2: OpenSky non-local complete cached at ROUTE_MISS_TTL",
        "callsign": "AAL_SKYT", "hex": "skyhex1", "plane": (36.0840, -115.1537),
        "opensky": {"origin": "DEN", "dest": "ORD"}, "airlabs1": None, "aeroapi": None,
        "expect": ("", "", "none"),
        "expect_cache_ttl": {"skyhex1": overhead.ROUTE_MISS_TTL},
    },
    {
        # A plausible non-local GA route is KEPT (prefer local, but don't strip a valid
        # non-local one) at the normal 1 h TTL — only the read-side geometry check busts
        # it if the plane later moves off the route.
        "name": "review2: FR24 GA non-local complete kept at ROUTE_TTL_DEFAULT",
        "callsign": "N999XY", "reg": "N999XY", "plane": (40.9, -96.0),  # on the DEN-ORD line
        "fr24": [{"origin": "DEN", "dest": "ORD"}],
        "expect": ("DEN", "ORD", "fr24"),
        "expect_cache_ttl": {"fr24:N999XY": overhead.ROUTE_TTL_DEFAULT},
    },
    {
        # A non-local origin-ONLY partial from a paid API must never overwrite an
        # already-committed LOCAL origin (the conflict-guard; the picker is the
        # backstop).  Invariant lock: the home origin survives.
        "name": "review2: committed local origin survives a non-local paid partial",
        "callsign": "XYZ123",                                  # non-scheduled → free APIs commit
        "adsbdb": {"origin": "LAS", "dest": ""},               # commits local origin LAS
        "airlabs1": {"origin": "PHX", "dest": ""},             # non-local origin-only partial
        "expect": ("LAS", "", "adsbdb"),
    },
    {
        # AirLabs allows usage past the nominal monthly limit, so a high local count
        # must NOT hard-stop the call — we probe and let AirLabs decide.  (Reverting to
        # a pre-call count stop would make airlabs1 calls == 0 and the route resolve
        # elsewhere / not at all.)
        "name": "review2: usage past nominal limit still probes AirLabs (no hard stop)",
        "callsign": "AAL_PROBE", "usage_value": 2000,          # well over the 1000 limit
        "airlabs1": {"origin": "LAS", "dest": "JFK"},          # AirLabs still serves a route
        "expect": ("LAS", "JFK", "airlabs"),
        "expect_calls": {"airlabs1": 1},                       # it CALLED despite the count
    },
    {
        # A charter/business-jet callsign (NOT a scheduled prefix, NOT an N-number
        # callsign) whose tail is only in the hex->reg cache must still reach FR24 §1
        # (GA, by registration) — and resolving there short-circuits the paid APIs.
        # Regression: _is_n_number was computed BEFORE the hex->reg fallback, so §1 was
        # skipped — the live EJM326 / NetJets case (Gulfstream, tail N326K, callsign EJMxxx).
        "name": "review2: FR24 GA via cached hex->reg (non-N-number charter callsign)",
        "callsign": "ZZQ500", "hex_reg": "N326K",              # tail from hex cache, not feed
        "fr24": [{"origin": "LAS", "dest": "VNY"}],
        "expect": ("LAS", "VNY", "fr24"),
        "expect_calls": {"fr24": 1, "aeroapi": 0, "airlabs1": 0},  # §1 resolves; paid skipped
    },
    # ── GUARANTEE: FR24 §5 is polled after AirLabs + AeroAPI return empty, whenever a
    #    tail is obtainable (feed / hex-cache / OpenSky-meta).  The FR24 library is
    #    registration-only, so the LAST scenario documents the irreducible no-tail limit. ──
    {
        "name": "fr24-after-paid: tail in FEED, both paid empty -> FR24 §5 polled",
        "callsign": "AAL901", "reg": "N901AA",
        "airlabs1": None, "aeroapi": None,
        "fr24": [{"origin": "LAS", "dest": "JFK"}],
        "expect": ("LAS", "JFK", "fr24"),
        "expect_calls": {"airlabs1": 1, "aeroapi": 1, "fr24": 1},  # paid tried, THEN FR24
    },
    {
        "name": "fr24-after-paid: tail only in CACHE, both paid empty -> FR24 §5 polled",
        "callsign": "AAL902", "hex_reg": "N902AA",
        "airlabs1": None, "aeroapi": None,
        "fr24": [{"origin": "LAS", "dest": "BOS"}],
        "expect": ("LAS", "BOS", "fr24"),
        "expect_calls": {"airlabs1": 1, "aeroapi": 1, "fr24": 1},
    },
    {
        "name": "fr24-after-paid: no feed-tail -> OpenSky-meta resolves it, FR24 §5 polled (poll 1)",
        "callsign": "AAL903", "opensky_meta_reg": "N903AA",   # proactively resolved tail
        "airlabs1": None, "aeroapi": None,
        "fr24": [{"origin": "LAS", "dest": "SEA"}],
        "expect": ("LAS", "SEA", "fr24"),
        "expect_calls": {"airlabs1": 1, "aeroapi": 1, "fr24": 1},
    },
    {
        "name": "fr24-after-paid: paid held NON-LOCAL, FR24 has LOCAL -> FR24 overrides",
        "callsign": "AAL904", "reg": "N904AA",
        "airlabs1": {"origin": "DEN", "dest": "ORD"},   # non-local (held, not committed)
        "aeroapi":  {"origin": "DEN", "dest": "ORD"},   # non-local (held)
        "fr24": [{"origin": "LAS", "dest": "JFK"}],      # local
        "expect": ("LAS", "JFK", "fr24"),
        "expect_calls": {"fr24": 1},
    },
    {
        "name": "fr24-after-paid: NO tail anywhere -> FR24 NOT callable (library is registration-only)",
        "callsign": "AAL905",                            # no reg, no hex_reg, no opensky_meta_reg
        "airlabs1": None, "aeroapi": None,
        "fr24": [{"origin": "LAS", "dest": "JFK"}],        # FR24 HAS data — but we can't query it
        "expect": ("", "", "none"),
        "expect_calls": {"fr24": 0},                       # documented, irreducible limit
    },
    # ── GA / charter path (FR24 §1, first-resort, by N-number registration) ──
    {
        "name": "fr24-ga: N-number callsign -> FR24 §1 by reg (no metadata needed)",
        "callsign": "N123AB", "reg": "N123AB",
        "fr24": [{"origin": "LAS", "dest": "VGT"}],
        "expect": ("LAS", "VGT", "fr24"),
        "expect_calls": {"fr24": 1},
    },
    {
        "name": "fr24-ga: charter (non-N-number callsign), tail in CACHE -> FR24 §1 polled",
        "callsign": "EJM326", "hex_reg": "N326K",        # NetJets-style; tail only in cache
        "fr24": [{"origin": "LAS", "dest": "VNY"}],
        "expect": ("LAS", "VNY", "fr24"),
        "expect_calls": {"fr24": 1, "airlabs1": 0, "aeroapi": 0},   # §1 short-circuits paid
    },
    {
        "name": "fr24-ga: charter, no feed-tail -> OpenSky-meta resolves it, FR24 §1 polled (poll 1)",
        "callsign": "EJM327", "opensky_meta_reg": "N327K",
        "fr24": [{"origin": "LAS", "dest": "SDL"}],
        "expect": ("LAS", "SDL", "fr24"),
        "expect_calls": {"fr24": 1},
    },
    {
        "name": "fr24-ga: §1 partial NON-LOCAL route is SERVED, not stripped (the POC case)",
        "callsign": "N700XY", "reg": "N700XY",
        "fr24": [{"origin": "POC", "dest": ""}],           # partial, non-local origin
        "expect": ("POC", "", "fr24"),
    },
    {
        # CROSS-SOURCE COMBINE: OpenSky supplies a trusted LOCAL origin (LAS->?) and
        # AirLabs supplies only the destination (?->JFK).  The live code merges them into
        # LAS->JFK ("opensky+airlabs").  This probes whether _select() (which picks ONE
        # candidate) preserves that merge.
        "name": "combine-check: local-origin (opensky) + dest-only (airlabs) -> LAS->JFK",
        "callsign": "XYZ123",
        "opensky": {"origin": "LAS", "dest": ""},
        "airlabs1": {"origin": "", "dest": "JFK"},
        "expect": ("LAS", "JFK", "opensky+airlabs"),
    },
    {
        # A scheduled LOCAL-origin route from a COORDLESS source (AirLabs returned no
        # lat/lng) must still get a 7-day resolved-cache entry, so future polls short-
        # circuit instead of re-walking adsbdb -> OpenSky -> AirLabs every time (the
        # JBU1756 LAS->EWR case).  The resolved READ trusts a local origin without coords.
        "name": "resolved-cache: coordless LOCAL scheduled route still caches (no chain re-walk)",
        "callsign": "AAL1756", "airlabs1": {"origin": "LAS", "dest": "EWR"},  # no coords
        "expect": ("LAS", "EWR", "airlabs"),
        "expect_cache_types": ["resolved"],
    },
    {
        # Issue A: when AirLabs-1 is OVER QUOTA it returns a 200-empty.  That empty is
        # meaningless (the backend was never consulted) and must NOT be cached — a cached
        # empty trips the §3b gate (_al1_cache_was_empty) into skipping the still-quota'd
        # AirLabs-2 next poll, leaking the lookup to the costlier AeroAPI.  Here AL-1 is
        # over quota, AL-2 has the route and WINS, and AeroAPI is never reached.
        "name": "Issue A: AL-1 over-quota -> AL-2 resolves (no AeroAPI leak), poison empty NOT cached",
        "callsign": "AAL8001",
        "airlabs1": None, "airlabs1_count": 1332,           # over the 1000 limit -> 200-empty
        "airlabs2": {"origin": "LAS", "dest": "ORD"},
        "aeroapi": {"origin": "LAS", "dest": "ORD"},        # available but must NOT be needed
        "expect": ("LAS", "ORD", "airlabs2"),
        "expect_calls": {"airlabs2": 1, "aeroapi": 0},      # AL-2 consulted & wins; AeroAPI not reached
        "expect_no_cache_key": ["airlabs:AAL8001"],         # over-quota empty must NOT be cached (the fix)
        "expect_backoff": [("airlabs", overhead.QUOTA_PROBE_BACKOFF_SECS)],
    },
    {
        # Control / no regression: a genuine WITHIN-quota empty IS still cached, and the
        # §3b optimization (skip AL-2 when AL-1 found nothing in the shared backend) holds.
        "name": "Issue A control: AL-1 within-quota empty IS cached, AL-2 correctly skipped",
        "callsign": "AAL8002",
        "airlabs1": None, "airlabs1_count": 1,              # within limit
        "airlabs2": None, "aeroapi": None,
        "expect": ("", "", "none"),
        "expect_cache_key": ["airlabs:AAL8002"],            # within-quota empty IS cached
        "expect_calls": {"airlabs2": 0},                    # shared-backend empty correctly skips AL-2
    },
    {
        # Phase-3 flip regression: an AeroAPI-resolved ARRIVAL (non-local origin -> home
        # airport) must still write the 7-day resolved cache.  The flip's _select candidate
        # for AeroAPI carries no inline coords, so coords are looked up from the harvested
        # airport table; without that the non-local-origin write guard skips the entry and
        # the paid chain re-bills every poll.
        "name": "flip-regression: AeroAPI arrival (non-local origin) still writes resolved cache",
        "callsign": "AAL7700", "airlabs1": None, "airlabs2": None,
        "aeroapi": {"origin": "JFK", "dest": "LAS",
                    "olat": 40.64, "olon": -73.78, "dlat": 36.08, "dlon": -115.15},
        "expect": ("JFK", "LAS", "aeroapi"),
        "expect_cache_types": ["resolved"],
    },
]


class GetRouteBaseline(unittest.TestCase):
    pass


def _mk(scn):
    def test(self):
        o, d, src, calls, sx = run_scenario(scn)
        self.assertEqual((o, d, src), scn["expect"],
                         f"\n{scn['name']}\n got={(o, d, src)} expected={scn['expect']}")
        for api, n in scn.get("expect_calls", {}).items():
            self.assertEqual(calls[api], n,
                             f"\n{scn['name']}: {api} called {calls[api]}x, expected {n}\n calls={calls}")
        # cache-TTL assertions: {cache_key: expected_ttl_secs}
        for key, ttl in scn.get("expect_cache_ttl", {}).items():
            matches = [w for w in sx["cache_writes"] if w["key"] == key]
            self.assertTrue(matches, f"\n{scn['name']}: no cache write for '{key}'\n writes={sx['cache_writes']}")
            self.assertAlmostEqual(matches[-1]["ttl"], ttl, delta=5,
                msg=f"\n{scn['name']}: cache '{key}' ttl={matches[-1]['ttl']} expected≈{ttl}")
        # backoff assertions: [(api, secs), ...]
        for api, secs in scn.get("expect_backoff", []):
            self.assertTrue(any(b["api"] == api and b["secs"] == secs for b in sx["backoffs"]),
                f"\n{scn['name']}: expected backoff ({api}, {secs}); got {sx['backoffs']}")
        # last-write-empty: the FINAL cache write for each key must be a negative entry
        # (empty origin+dest) — pins that an implausible route ends up negatively cached
        # instead of left to bust+refetch every poll.
        for key in scn.get("expect_cache_empty", []):
            matches = [w for w in sx["cache_writes"] if w["key"] == key]
            self.assertTrue(matches, f"\n{scn['name']}: no cache write for '{key}'")
            self.assertEqual((matches[-1]["origin"], matches[-1]["dest"]), ("", ""),
                f"\n{scn['name']}: last write for '{key}' = "
                f"{(matches[-1]['origin'], matches[-1]['dest'])}, expected empty (negative)")
        # cache-type-absent: these cache_types must NOT be written at all
        for ctype in scn.get("expect_no_cache_types", []):
            hits = [w for w in sx["cache_writes"] if w["type"] == ctype]
            self.assertFalse(hits, f"\n{scn['name']}: unexpected '{ctype}' cache write: {hits}")
        # cache-type-present: these cache_types MUST be written
        for ctype in scn.get("expect_cache_types", []):
            hits = [w for w in sx["cache_writes"] if w["type"] == ctype]
            self.assertTrue(hits, f"\n{scn['name']}: expected a '{ctype}' cache write, got none")
        # cache-key-absent: these exact keys must NOT be written (Issue A: an over-quota
        # empty must not be cached, or it poisons the §3b secondary-key gate next poll).
        for key in scn.get("expect_no_cache_key", []):
            hits = [w for w in sx["cache_writes"] if w["key"] == key]
            self.assertFalse(hits, f"\n{scn['name']}: unexpected cache write for key '{key}': {hits}")
        # cache-key-present: these exact keys MUST be written.
        for key in scn.get("expect_cache_key", []):
            hits = [w for w in sx["cache_writes"] if w["key"] == key]
            self.assertTrue(hits, f"\n{scn['name']}: expected a cache write for key '{key}', got none")
        # paid-miss recorded? (True/False)
        if "expect_paid_miss" in scn:
            self.assertEqual(sx["paid_miss_set"], scn["expect_paid_miss"],
                f"\n{scn['name']}: paid_miss_set={sx['paid_miss_set']} expected={scn['expect_paid_miss']}")
    return test


for i, s in enumerate(SCENARIOS):
    setattr(GetRouteBaseline, f"test_{i:02d}_{s['name'].split(':')[0].replace(' ', '_')}", _mk(s))


class RunTestLookupFidelity(unittest.TestCase):
    """run_test_lookup(use_cache=False) must follow the SAME logic as a real flight.

    Regression for the no-cache divergence: a commercial flight whose live ADS-B feed
    omits the tail must still reach FR24 §5 via the permanent hex→reg cache, which the
    web diagnostic now resolves BEFORE enabling the cache-bypass.  Before the fix the
    bypassed reg read yielded "" and FR24 §5 was skipped, so the diagnostic reported
    'none' while the real flight resolved the route.
    """

    def test_no_cache_commercial_tailless_resolves_fr24_via_reg_cache(self):
        import tempfile
        live = {"ac": [{"hex": "a1b2c3", "r": "",            # feed carries NO tail
                        "lat": 36.0840, "lon": -115.1537,
                        "alt_baro": 9000, "baro_rate": 1200,
                        "desc": "AIRBUS A220-300"}]}

        def fake_get(url, params=None, headers=None, timeout=None, **kw):
            if "api.airplanes.live/v2/callsign/" in url:
                return FakeResp(200, live)
            # every route + type API returns empty so ONLY FR24 §5 can resolve
            if "adsbdb.com/v0/callsign" in url:           return _resp_adsbdb(None)
            if "opensky-network.org/api/flights" in url:  return _resp_opensky(None)
            if "airlabs.co" in url:                        return _resp_airlabs(None)
            if "aeroapi.flightaware.com" in url:           return _resp_aeroapi(None)
            return FakeResp(404, {})

        fr24_api = _fr24_api_for([{"origin": "LAS", "dest": "SYR"}])

        # The reg cache holds the tail.  The mock honors the REAL bypass semantics so
        # the divergence is reproduced faithfully: under bypass it returns "" (exactly
        # like production's _cache_db_get_reg), so the ONLY way FR24 §5 sees a tail is
        # run_test_lookup resolving it BEFORE the bypass is enabled.
        def fake_get_reg(hex_code):
            return "" if getattr(overhead._cache_bypass, "on", False) else "N218BZ"

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as _tf:
            _disp = _tf.name
        patches = {
            "requests": mock.MagicMock(),
            "_get_fr24_api": mock.MagicMock(return_value=fr24_api),
            "_cache_db_get_route": mock.MagicMock(return_value=None),
            "_cache_db_set_route": mock.MagicMock(),
            "_cache_db_delete_route": mock.MagicMock(),
            "_cache_db_check_paid_miss": mock.MagicMock(return_value=False),
            "_cache_db_set_paid_miss": mock.MagicMock(),
            "_cache_db_get_aircraft": mock.MagicMock(return_value=None),
            "_cache_db_set_aircraft": mock.MagicMock(),
            "_cache_db_get_reg": mock.MagicMock(side_effect=fake_get_reg),
            "_cache_db_set_reg": mock.MagicMock(),
            "_in_backoff": mock.MagicMock(return_value=False),
            "_match_override": mock.MagicMock(return_value=None),
            "_get_opensky_token": mock.MagicMock(return_value="tok"),
            "_read_usage": mock.MagicMock(return_value={"value": 0, "period_start": "2026-06-01"}),
            "_write_usage": mock.MagicMock(),
            "_airlabs_increment": mock.MagicMock(return_value=1),
            "_airlabs2_increment": mock.MagicMock(return_value=1),
            "_aeroapi_increment": mock.MagicMock(return_value=None),
            "_record_api_stat": mock.MagicMock(),
            "_set_backoff": mock.MagicMock(),
            "TEST_DISPLAY_FILE": _disp,
        }
        patches["requests"].get.side_effect = fake_get
        try:
            with mock.patch.multiple(overhead, **patches), \
                 mock.patch.object(overhead.os.path, "exists", return_value=False):
                res = overhead.run_test_lookup("MXY243", use_cache=False)
        finally:
            for _p in (_disp, _disp + ".tmp"):
                try:
                    os.remove(_p)
                except OSError:
                    pass

        self.assertEqual(res["route_source"], "fr24",
                         f"\n route_source={res['route_source']} steps={res.get('steps')}")
        self.assertEqual((res["final_origin"], res["final_destination"]), ("LAS", "SYR"))
        # the thread-local bypass must not leak True after the call
        self.assertFalse(getattr(overhead._cache_bypass, "on", False))


class SelectFunction(unittest.TestCase):
    """Direct tests for the centralized _select() — the single route-selection point the
    structural refactor is built on.  Sources only PRODUCE candidates; _select() decides.
    _select() is now the sole route authority in get_route() (Phase-3 flip, made permanent)."""

    def setUp(self):
        self._lp = mock.patch.object(overhead, "_LOCAL_AIRPORTS",
                                     frozenset({"LAS", "VGT", "HSH"}))
        self._lp.start()

    def tearDown(self):
        self._lp.stop()

    def _c(self, origin, dest, source, olat=None, olon=None, dlat=None, dlon=None, is_live=True):
        return overhead._Cand(origin, dest, olat, olon, dlat, dlon, source, is_live)

    def test_local_complete_beats_nonlocal_complete(self):
        best = overhead._select([
            self._c("DEN", "ORD", "airlabs"),   # non-local complete, TOP priority
            self._c("LAS", "JFK", "adsbdb"),     # local complete, lowest priority
        ], None, None)
        self.assertEqual((best.origin, best.dest, best.source), ("LAS", "JFK", "adsbdb"))

    def test_local_partial_beats_nonlocal_complete(self):
        best = overhead._select([
            self._c("DEN", "ORD", "aeroapi"),    # non-local complete
            self._c("LAS", "", "opensky"),        # local partial
        ], None, None)
        self.assertEqual((best.origin, best.dest, best.source), ("LAS", "", "opensky"))

    def test_local_complete_upgrades_local_partial(self):
        best = overhead._select([
            self._c("LAS", "", "opensky"),        # local partial
            self._c("LAS", "JFK", "adsbdb"),       # local complete
        ], None, None)
        self.assertEqual((best.origin, best.dest), ("LAS", "JFK"))

    def test_source_priority_breaks_ties_within_tier(self):
        best = overhead._select([
            self._c("DEN", "ORD", "fr24"),         # non-local complete, prio 3
            self._c("SEA", "ATL", "airlabs"),      # non-local complete, prio 0
            self._c("MIA", "BOS", "adsbdb"),       # non-local complete, prio 4
        ], None, None)
        self.assertEqual(best.source, "airlabs")   # most-trusted source wins the tier

    def test_geometry_implausible_filtered_out(self):
        # plane over LAS; a JFK->BOS leg (own coords) is implausible -> dropped -> None
        best = overhead._select([
            self._c("JFK", "BOS", "airlabs", 40.64, -73.78, 42.37, -71.01),
        ], 36.0840, -115.1537)
        self.assertIsNone(best)

    def test_empty_and_blank_candidates_return_none(self):
        self.assertIsNone(overhead._select([], None, None))
        self.assertIsNone(overhead._select([self._c("", "", "airlabs")], None, None))


class CacheRoundTrip(unittest.TestCase):
    """Exercise the REAL SQLite cache helpers against a temp DB.  The rest of the suite
    mocks these away, so this is the only place write -> read -> expire -> bypass is
    verified end to end (the bug class behind the 'Issue B' stale-resolved entries)."""

    def setUp(self):
        import sqlite3, tempfile
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self._conn = sqlite3.connect(self._tmp.name, check_same_thread=False)
        self._conn.execute(
            """CREATE TABLE cache (
                   key TEXT NOT NULL, cache_type TEXT NOT NULL,
                   origin TEXT NOT NULL DEFAULT '', dest TEXT NOT NULL DEFAULT '',
                   olat REAL, olon REAL, dlat REAL, dlon REAL,
                   value TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
                   expires_at INTEGER NOT NULL DEFAULT 0,
                   PRIMARY KEY (key, cache_type))""")
        self._conn.commit()
        # The cache CRUD helpers were extracted to utilities/cache.py; they read the
        # connection from THAT module (overhead injects it via cache.bind()).  Patch it
        # there — patching overhead._cache_conn would no longer reach the helpers.
        self._orig_conn = overhead.cache._cache_conn
        overhead.cache._cache_conn = self._conn
        overhead._cache_bypass.on = False        # shared threading.local — cache sees it too

    def tearDown(self):
        overhead.cache._cache_conn = self._orig_conn
        overhead._cache_bypass.on = False
        self._conn.close()
        try:
            os.remove(self._tmp.name)
        except OSError:
            pass

    def test_write_then_read_roundtrip(self):
        exp = int(time.time()) + 3600
        overhead._cache_db_set_route("AAL1", "resolved", "LAS", "JFK",
                                     36.08, -115.15, 40.64, -73.78, exp, source="adsbdb")
        row = overhead._cache_db_get_route("AAL1", "resolved")
        self.assertIsNotNone(row)
        self.assertEqual((row[0], row[1], row[6]), ("LAS", "JFK", "adsbdb"))

    def test_expired_entry_is_invisible(self):
        past = int(time.time()) - 10
        overhead._cache_db_set_route("AAL2", "resolved", "LAS", "JFK",
                                     None, None, None, None, past, source="adsbdb")
        self.assertIsNone(overhead._cache_db_get_route("AAL2", "resolved"))

    def test_bypass_masks_reads_without_deleting(self):
        exp = int(time.time()) + 3600
        overhead._cache_db_set_route("AAL3", "resolved", "LAS", "JFK",
                                     None, None, None, None, exp)
        overhead._cache_bypass.on = True
        self.assertIsNone(overhead._cache_db_get_route("AAL3", "resolved"))
        overhead._cache_bypass.on = False
        self.assertIsNotNone(overhead._cache_db_get_route("AAL3", "resolved"))

    def test_upsert_keeps_single_row(self):
        exp = int(time.time()) + 3600
        overhead._cache_db_set_route("AAL4", "resolved", "LAS", "JFK",
                                     None, None, None, None, exp)
        overhead._cache_db_set_route("AAL4", "resolved", "LAS", "ORD",
                                     None, None, None, None, exp)
        n = self._conn.execute(
            "SELECT COUNT(*) FROM cache WHERE key='AAL4' AND cache_type='resolved'"
        ).fetchone()[0]
        self.assertEqual(n, 1)
        self.assertEqual(overhead._cache_db_get_route("AAL4", "resolved")[1], "ORD")

    def test_delete_removes_entry(self):
        exp = int(time.time()) + 3600
        overhead._cache_db_set_route("AAL5", "resolved", "LAS", "JFK",
                                     None, None, None, None, exp)
        overhead._cache_db_delete_route("AAL5", "resolved")
        self.assertIsNone(overhead._cache_db_get_route("AAL5", "resolved"))


class RoutePlausibleGeometry(unittest.TestCase):
    """Direct unit tests for the _route_plausible detour-ratio guard — the core anti-stale-
    route check, otherwise exercised only indirectly through full get_route scenarios."""

    LAS = (36.08, -115.15)
    JFK = (40.64, -73.78)

    def test_missing_coords_assumed_plausible(self):
        self.assertTrue(overhead._route_plausible(None, None, *self.LAS, *self.JFK))

    def test_same_airport_rejected(self):
        # origin == dest -> zero-length route -> no valid flight path
        self.assertFalse(overhead._route_plausible(*self.LAS, *self.LAS, *self.LAS))

    def test_short_hop_assumed_plausible(self):
        # ~0.3 km apart -> below the short-hop floor where geometry isn't reliable
        self.assertTrue(overhead._route_plausible(0.0, 0.0,
                                                  36.000, -115.000, 36.003, -115.000))

    def test_on_route_is_plausible(self):
        # plane sitting at the origin -> detour ratio 1.0
        self.assertTrue(overhead._route_plausible(*self.LAS, *self.LAS, *self.JFK))

    def test_far_off_route_is_implausible(self):
        # LAS->JFK route, but the plane is off west Africa -> enormous detour ratio
        self.assertFalse(overhead._route_plausible(0.0, 0.0, *self.LAS, *self.JFK))


if __name__ == "__main__":
    print("=== get_route baseline (result + live API call counts) ===")
    for s in SCENARIOS:
        try:
            o, d, src, calls, sx = run_scenario(s)
            res = (o, d, src)
            ok = "OK " if res == s["expect"] else "DIFF"
            cs = " ".join(f"{k}={v}" for k, v in calls.items() if v) or "(none)"
            mark = ""
            for api, n in s.get("expect_calls", {}).items():
                if calls[api] != n:
                    mark = f"  CALLS-DIFF {api}={calls[api]}!={n}"
            print(f"  {ok}  {s['name'][:50]:50} {str(res):26} calls[{cs}]{mark}")
        except Exception as e:
            import traceback
            print(f"  ERR  {s['name'][:50]:50} {type(e).__name__}: {e}")
            traceback.print_exc()

    # Run the real assertions (results + call counts + cache TTL/empty + backoff +
    # paid-miss + run_test_lookup fidelity) and exit non-zero on any failure, so the
    # deploy gate actually catches regressions instead of always exiting 0.
    print("\n=== unittest assertions ===")
    _suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    _result = unittest.TextTestRunner(verbosity=1).run(_suite)
    sys.exit(0 if _result.wasSuccessful() else 1)
