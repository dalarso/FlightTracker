"""Focused regression tests for the 'overhead' fix bucket.

These pin the behavioral fixes applied to utilities/overhead.py:

  #1  free-source (adsbdb/OpenSky) candidates carry the LOCAL-ORIGIN + non-commercial
      trust gate into the candidate model (the inline chain no longer decides the route)
  #2  the REAL Overhead background-thread hand-off contract (atomic swap / success-gate)
  #3  _select() merge can't synthesize a same-airport (LAS->LAS) route
  #5  a coord-bearing LOCAL-origin resolved-cache entry is geometry-checked, not trusted blind
  #6  per-source codes are normalised (strip/upper + BLANK_FIELDS) at candidate construction
  #7  a merged partial route is re-validated for geometry before it can win
  #16 the (date, callsign) enrich-dedup map is pruned of stale keys at day rollover
  #17 OpenSky route cache entries are namespaced under a 'hex:' prefix

Hardware (rgbmatrix / RPi.GPIO) is stubbed before import, and FT_DATA_DIR is redirected to
a throwaway temp dir so the repo's ft_flights.db is never touched.  Run:

    python3 tests/test_overhead_fixes.py
"""
import os
import sys
import tempfile
import types
import unittest
from unittest import mock
from pathlib import Path

# ── Redirect all data files (SQLite DB, usage/override JSON) to a throwaway dir BEFORE
#    importing overhead, so the import-time _init_db never touches the real repo DB. ──
_TMP_DATA_DIR = tempfile.mkdtemp(prefix="ft_overhead_test_")
os.environ["FT_DATA_DIR"] = _TMP_DATA_DIR

# Stub the LED-hardware modules the same way the rest of the suite does.
for _m, _v in (("rgbmatrix", mock.MagicMock()), ("rgbmatrix.graphics", mock.MagicMock()),
               ("RPi", mock.MagicMock()), ("RPi.GPIO", mock.MagicMock())):
    sys.modules.setdefault(_m, _v)

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "utilities"))

import overhead  # noqa: E402


def _c(origin, dest, source, olat=None, olon=None, dlat=None, dlon=None):
    return overhead._Cand(origin, dest, olat, olon, dlat, dlon, source)


class NormCode(unittest.TestCase):
    """#6 — codes are normalised once at candidate construction."""

    def test_strip_and_upper(self):
        self.assertEqual(overhead._norm_code("  las "), "LAS")

    def test_blank_fields_mapped_to_empty(self):
        for junk in ("", "N/A", "n/a", "NONE", " none "):
            self.assertEqual(overhead._norm_code(junk), "")

    def test_none_input(self):
        self.assertEqual(overhead._norm_code(None), "")

    def test_same_airport_after_norm_is_rejected_by_select(self):
        # ' las ' vs 'LAS' must be treated as the SAME airport once normalised.
        with mock.patch.object(overhead, "_LOCAL_AIRPORTS", frozenset({"LAS"})):
            cand = _c(overhead._norm_code(" las "), overhead._norm_code("LAS"), "airlabs")
            self.assertFalse(overhead._cand_plausible(cand, None, None))


class SelectMergeGuards(unittest.TestCase):
    """#3 + #7 — the cross-source merge can't emit a degenerate or implausible route."""

    def setUp(self):
        self._lp = mock.patch.object(overhead, "_LOCAL_AIRPORTS", frozenset({"LAS", "VGT"}))
        self._lp.start()

    def tearDown(self):
        self._lp.stop()

    def test_merge_never_synthesizes_same_airport(self):
        # local LAS->? merged with ?->LAS must NOT become LAS->LAS (#3).  Either partial may
        # win the source-priority tie; the invariant is only that the result isn't degenerate
        # and isn't a completed same-airport route.
        best = overhead._select([_c("LAS", "", "opensky"),
                                 _c("", "LAS", "airlabs")], None, None)
        self.assertIsNotNone(best)
        self.assertFalse(best.origin and best.dest and best.origin.upper() == best.dest.upper())
        # falls back to a single local partial rather than emitting garbage
        self.assertTrue((not best.origin) or (not best.dest))
        self.assertIn("LAS", (best.origin, best.dest))

    def test_merge_completes_when_donor_is_distinct(self):
        # A legitimate LAS->? + ?->JFK merge still works (no coords -> plausible).
        best = overhead._select([_c("LAS", "", "opensky"),
                                 _c("", "JFK", "airlabs")], None, None)
        self.assertEqual((best.origin, best.dest), ("LAS", "JFK"))
        self.assertIn("+", best.source)

    def test_merge_rejected_when_combined_route_implausible(self):
        # Plane sits at LAS; donor supplies a dest whose real coords make the combined route
        # geometrically implausible relative to the plane -> merge is abandoned (#7).  Donor
        # coords are now carried into the merged candidate so _route_plausible can fire on
        # them directly (no dependence on the code resolving via _airport_coords).
        las = (36.08, -115.15)
        jfk = (40.64, -73.78)
        # Plane sits far off the LAS->JFK great circle (over South America) and far from both
        # endpoints, so the detour ratio blows past the threshold.
        best = overhead._select([
            _c("LAS", "", "opensky", olat=las[0], olon=las[1]),
            _c("", "JFK", "airlabs", dlat=jfk[0], dlon=jfk[1]),
        ], -30.0, -60.0)
        # merge must be abandoned -> keep the local partial, never the implausible combo
        self.assertEqual((best.origin, best.dest), ("LAS", ""))


class FreeSourceTrustGate(unittest.TestCase):
    """#1 — the LOCAL-ORIGIN + non-commercial gate travels into the candidate model.

    The gate now lives at the candidate-append site in get_route(); here we assert the
    selection semantics the gate protects: a LOCAL stale free route must NOT be allowed to
    outrank a live paid non-local one.  We simulate 'gate applied' (free cand dropped) vs
    'gate skipped' (free cand present) and confirm only the gated set yields the paid route.
    """

    def setUp(self):
        self._lp = mock.patch.object(overhead, "_LOCAL_AIRPORTS", frozenset({"LAS"}))
        self._lp.start()

    def tearDown(self):
        self._lp.stop()

    def test_ungated_local_free_route_wrongly_beats_paid(self):
        # Without the gate a stale LOCAL adsbdb route would win on tier (local beats complete).
        best = overhead._select([_c("SYR", "CHS", "airlabs"),   # live paid, non-local
                                 _c("LAS", "JFK", "adsbdb")],    # stale free, LOCAL
                                None, None)
        self.assertEqual(best.source, "adsbdb")  # documents the pre-fix hazard

    def test_gated_candidate_set_yields_paid_route(self):
        # The fix drops the untrusted free candidate before _select, so the paid route wins.
        best = overhead._select([_c("SYR", "CHS", "airlabs")], None, None)
        self.assertEqual((best.origin, best.dest, best.source), ("SYR", "CHS", "airlabs"))


class ResolvedCacheLocalGeometry(unittest.TestCase):
    """#5 — a coord-bearing LOCAL-origin resolved entry is geometry-checked before trust."""

    def setUp(self):
        self._lp = mock.patch.object(overhead, "_LOCAL_AIRPORTS", frozenset({"LAS"}))
        self._lp.start()

    def tearDown(self):
        self._lp.stop()

    def _drive(self, resolved_row, plane_lat, plane_lon):
        """Run get_route far enough to exercise the §0.5 resolved-cache block, returning
        the busts seen.  All downstream sources are stubbed to return nothing so the route
        falls through to ('','','none') when the cached entry is busted."""
        busts = []

        def _fake_get(key, ctype):
            if ctype == "resolved":
                return resolved_row
            return None

        with mock.patch.object(overhead, "_cache_db_get_route", side_effect=_fake_get), \
             mock.patch.object(overhead, "_cache_db_delete_route",
                               side_effect=lambda k, t: busts.append((k, t))), \
             mock.patch.object(overhead, "_route_ttl",
                               return_value=overhead.ROUTE_TTL_SCHEDULED), \
             mock.patch.object(overhead, "fetch_flights", return_value=[]), \
             mock.patch.object(overhead, "_match_override", return_value=None), \
             mock.patch.object(overhead, "_query_adsbdb",
                               return_value=("", "", None, None, None, None, "adsbdb")), \
             mock.patch.object(overhead, "_query_opensky", return_value=("", "", "opensky")), \
             mock.patch.object(overhead, "_query_airlabs",
                               return_value=("", "", None, None, None, None, "airlabs", 0)), \
             mock.patch.object(overhead, "_cache_db_set_route"), \
             mock.patch.object(overhead, "_cache_db_check_paid_miss", return_value=False), \
             mock.patch.object(overhead, "_get_fr24_route",
                               return_value=("", "", "fr24"), create=True):
            try:
                res = overhead.get_route("abc123", "AAL100", 0, plane_lat, plane_lon)
            except Exception:
                res = None
        return res, busts

    def test_local_entry_on_route_is_trusted(self):
        # dest coords present and the plane sits on the LAS->JFK path -> trusted, no bust.
        # LAS ~ (36.08,-115.15), JFK ~ (40.64,-73.78); plane between them.
        row = ("LAS", "JFK", 36.08, -115.15, 40.64, -73.78, "airlabs")
        res, busts = self._drive(row, 38.0, -95.0)
        self.assertEqual(busts, [])
        self.assertIsNotNone(res)
        self.assertEqual((res[0], res[1]), ("LAS", "JFK"))

    def test_local_entry_off_route_is_busted(self):
        # Same cached LAS->JFK but the plane is nowhere near that path -> geometry busts it.
        row = ("LAS", "JFK", 36.08, -115.15, 40.64, -73.78, "airlabs")
        res, busts = self._drive(row, -33.9, 151.2)  # over Sydney — way off LAS->JFK
        self.assertTrue(busts, "expected the stale local-origin entry to be busted")

    def test_local_entry_without_coords_keeps_fast_path(self):
        # No dest coords stored -> _route_plausible returns True -> trusted unconditionally.
        row = ("LAS", "JFK", None, None, None, None, "airlabs")
        res, busts = self._drive(row, -33.9, 151.2)
        self.assertEqual(busts, [])
        self.assertEqual((res[0], res[1]), ("LAS", "JFK"))


class EnrichDedupPrune(unittest.TestCase):
    """#16 — stale (date, callsign) enrich keys are pruned at day rollover."""

    def test_yesterday_keys_dropped_on_rollover(self):
        with mock.patch.object(overhead, "_db_conn", None):
            overhead._last_enrich_written.clear()
            overhead._last_enrich_written[("2026-06-10", "AAL1")] = ("x", "y", "z")
            overhead._last_enrich_written[("2026-06-11", "AAL2")] = ("x", "y", "z")
            # Force a rollover to a NEW day.
            overhead._stats_last_date = "2026-06-10"
            with mock.patch("overhead.datetime") as _dt:
                _dt.now.return_value.strftime.return_value = "2026-06-12"
                overhead._record_flight_stat("UAL9", "B738", "LAS", "JFK")
            keys = set(overhead._last_enrich_written.keys())
            # only-today keys survive; yesterday's are gone (none are today's here -> empty)
            self.assertTrue(all(k[0] == "2026-06-12" for k in keys))
            self.assertNotIn(("2026-06-10", "AAL1"), overhead._last_enrich_written)
            self.assertNotIn(("2026-06-11", "AAL2"), overhead._last_enrich_written)


class OpenSkyCacheNamespace(unittest.TestCase):
    """#17 — OpenSky route cache reads/writes use the 'hex:' prefix."""

    def test_query_opensky_uses_hex_prefix(self):
        seen = {}

        def _fake_get(key, ctype):
            seen["get"] = key
            return None  # force the live path

        with mock.patch.object(overhead, "_cache_db_get_route", side_effect=_fake_get), \
             mock.patch.object(overhead, "_cache_db_set_route",
                               side_effect=lambda k, *a, **kw: seen.setdefault("set", k)), \
             mock.patch.object(overhead, "_in_backoff", return_value=False), \
             mock.patch.object(overhead, "OPENSKY_CLIENT_ID", "cid"), \
             mock.patch.object(overhead, "OPENSKY_CLIENT_SECRET", "secret"), \
             mock.patch.object(overhead, "_get_opensky_token", return_value="tok"), \
             mock.patch.object(overhead, "_session") as _sess, \
             mock.patch("os.path.exists", return_value=False):
            _sess.get.return_value = types.SimpleNamespace(status_code=200, json=lambda: [])
            overhead._query_opensky("a1b2c3", "AAL1", "", "", 1_000_000)
        self.assertEqual(seen.get("get"), "hex:a1b2c3")
        self.assertEqual(seen.get("set"), "hex:a1b2c3")


class OverheadHandoff(unittest.TestCase):
    """#2 — the real Overhead background-thread hand-off contract.

    _grab_data is driven synchronously (called directly, not via the daemon thread) so the
    publish/success-gate is deterministic.  Pins: a thrown fetch must NOT publish or set
    new_data; a successful (even empty) fetch publishes and sets new_data exactly once;
    reading .data clears new_data; grab_data() is single-flighted while processing.
    """

    def _make(self):
        return overhead.Overhead()

    def test_thrown_fetch_does_not_publish(self):
        o = self._make()
        o._data = [{"sentinel": True}]   # pre-existing good data must survive
        with mock.patch.object(overhead, "_purge_expired_cache"), \
             mock.patch.object(overhead, "fetch_flights", side_effect=RuntimeError("down")), \
             mock.patch.object(overhead, "_rotate_log_if_needed"):
            o._grab_data()
        self.assertFalse(o.new_data, "a thrown fetch must not signal new data")
        self.assertEqual(o._data, [{"sentinel": True}], "data must be left unchanged")
        self.assertFalse(o.processing, "_processing must be reset after an exception")

    def test_successful_empty_fetch_publishes_once(self):
        o = self._make()
        with mock.patch.object(overhead, "_purge_expired_cache"), \
             mock.patch.object(overhead, "fetch_flights", return_value=[]), \
             mock.patch.object(overhead, "_rotate_log_if_needed"), \
             mock.patch.object(overhead, "_pathlib") as _pl:
            _pl.Path.return_value.read_text.side_effect = FileNotFoundError()
            o._grab_data()
        self.assertTrue(o.new_data, "an empty-but-successful fetch still publishes")
        # reading .data clears new_data so check_for_loaded_data won't re-fire
        _ = o.data
        self.assertFalse(o.new_data)
        self.assertFalse(o.processing)

    def test_grab_data_single_flighted_while_processing(self):
        o = self._make()
        with o._lock:
            o._processing = True   # simulate an in-flight poll
        with mock.patch.object(overhead, "Thread") as _T:
            o.grab_data()          # must no-op, not spawn a second worker
            _T.assert_not_called()

    def test_grab_data_resets_processing_if_thread_fails(self):
        o = self._make()
        with mock.patch.object(overhead, "Thread") as _T:
            _T.return_value.start.side_effect = RuntimeError("can't spawn")
            with self.assertRaises(RuntimeError):
                o.grab_data()
        self.assertFalse(o.processing, "_processing must not wedge True on thread-start failure")


class DepartureSignal(unittest.TestCase):
    """Evidence for the inbound-leg veto: a clear climb, or OpenSky's local departure
    airport unless the plane is clearly descending."""

    def setUp(self):
        self._p = mock.patch.object(overhead, "_LOCAL_AIRPORTS", frozenset({"LAS", "VGT", "HSH"}))
        self._p.start()
        self.addCleanup(self._p.stop)

    def test_climb_is_evidence(self):
        self.assertIn("climbing", overhead._departure_signal(1500))

    def test_opensky_local_origin_is_evidence(self):
        self.assertIn("LAS", overhead._departure_signal(0, "LAS"))

    def test_descent_overrides_opensky(self):
        self.assertEqual(overhead._departure_signal(-1000, "LAS"), "")

    def test_no_evidence(self):
        self.assertEqual(overhead._departure_signal(0, ""), "")
        self.assertEqual(overhead._departure_signal(150, "SFO"), "")    # level-ish, non-local
        self.assertEqual(overhead._departure_signal(None, ""), "")
        self.assertEqual(overhead._departure_signal("bogus", ""), "")

    def test_climb_far_from_home_is_not_evidence(self):
        home = overhead.LOCATION_DEFAULT
        self.assertIn("climbing", overhead._departure_signal(2000, "", home[0] + 0.1, home[1]))
        self.assertEqual(overhead._departure_signal(2000, "", home[0] + 5, home[1]), "")
        # ...but OpenSky's local departure airport still counts anywhere
        self.assertIn("LAS", overhead._departure_signal(2000, "LAS", home[0] + 5, home[1]))

    def test_global_mode_never_signals(self):
        with mock.patch.object(overhead, "_LOCAL_AIRPORTS", frozenset()):
            self.assertEqual(overhead._departure_signal(3000, "LAS"), "")

    def test_inbound_shape(self):
        self.assertTrue(overhead._is_inbound_to_local("ORD", "LAS"))
        self.assertTrue(overhead._is_inbound_to_local("", "LAS"))       # ?->LAS
        self.assertFalse(overhead._is_inbound_to_local("VGT", "LAS"))   # local -> local
        self.assertFalse(overhead._is_inbound_to_local("LAS", "ORD"))
        self.assertFalse(overhead._is_inbound_to_local("ORD", "DEN"))
        self.assertFalse(overhead._is_inbound_to_local("LAS", ""))


class IcaoToIata(unittest.TestCase):
    """KHND is Henderson Executive (IATA HSH) — stripping the K made it Tokyo's HND."""

    def test_exceptions(self):
        self.assertEqual(overhead.icao_to_iata("KHND"), "HSH")
        self.assertEqual(overhead.icao_to_iata("KBVU"), "BLD")

    def test_regular_strip_unchanged(self):
        self.assertEqual(overhead.icao_to_iata("KLAS"), "LAS")
        self.assertEqual(overhead.icao_to_iata("KVGT"), "VGT")
        self.assertEqual(overhead.icao_to_iata("CYYZ"), "YYZ")
        self.assertEqual(overhead.icao_to_iata("EGLL"), "EGLL")
        self.assertEqual(overhead.icao_to_iata(""), "")


class AeroapiPickFlight(unittest.TestCase):
    """The AeroAPI picker takes the leg in the air, never a later leg / another day's."""

    NOW = 1_800_000_000

    def _iso(self, offset):
        import time as _t
        return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(self.NOW + offset))

    def _f(self, name, **kw):
        return {"ident": name, **kw}

    def test_airborne_beats_later_scheduled_leg(self):
        flights = [
            self._f("tomorrow", scheduled_out=self._iso(86400)),
            self._f("next-leg", scheduled_out=self._iso(4 * 3600)),
            self._f("airborne", actual_off=self._iso(-900), scheduled_out=self._iso(-1500)),
            self._f("landed", actual_off=self._iso(-6 * 3600), actual_on=self._iso(-3 * 3600)),
        ]
        self.assertEqual(overhead._aeroapi_pick_flight(flights, now=self.NOW)["ident"], "airborne")

    def test_cancelled_airborne_is_ignored(self):
        flights = [self._f("x", actual_off=self._iso(-600), cancelled=True)]
        self.assertIsNone(overhead._aeroapi_pick_flight(flights, now=self.NOW))

    def test_no_takeoff_event_uses_nearest_departure_in_window(self):
        flights = [
            self._f("far", scheduled_out=self._iso(5 * 3600)),
            self._f("near", estimated_out=self._iso(-20 * 60)),
        ]
        self.assertEqual(overhead._aeroapi_pick_flight(flights, now=self.NOW)["ident"], "near")

    def test_nothing_near_now_returns_none(self):
        flights = [
            self._f("tomorrow", scheduled_out=self._iso(86400)),
            self._f("landed", actual_off=self._iso(-7200), actual_on=self._iso(-600)),
        ]
        self.assertIsNone(overhead._aeroapi_pick_flight(flights, now=self.NOW))

    def test_empty(self):
        self.assertIsNone(overhead._aeroapi_pick_flight([], now=self.NOW))
        self.assertIsNone(overhead._aeroapi_pick_flight(None, now=self.NOW))


class Dump1090Fallback(unittest.TestCase):
    """The fallback feed probes the common aircraft.json paths and parses both field sets."""

    def test_old_mutability_field_names(self):
        f = overhead.Flight.from_dump1090({"hex": "a1b2c3", "lat": 36.1, "lon": -115.2,
                                           "altitude": 7200, "vert_rate": 1984, "flight": "SWA1 "})
        self.assertEqual((f.altitude, f.vertical_speed, f.callsign), (7200, 1984, "SWA1"))

    def test_readsb_field_names_and_ground(self):
        f = overhead.Flight.from_dump1090({"hex": "a1b2c3", "lat": 36.1, "lon": -115.2,
                                           "alt_baro": 9000, "baro_rate": -640})
        self.assertEqual((f.altitude, f.vertical_speed), (9000, -640))
        g = overhead.Flight.from_dump1090({"hex": "a1b2c3", "lat": 36.1, "lon": -115.2,
                                           "alt_baro": "ground"})
        self.assertEqual(g.altitude, 0)

    def test_probes_paths_and_remembers_the_working_one(self):
        good = f"http://{overhead.RECEIVER_HOST}/dump1090/data/aircraft.json"
        seen = []

        def fake_get(url, timeout=None, **kw):
            seen.append(url)
            if url == good:
                r = mock.MagicMock(status_code=200)
                r.json.return_value = {"aircraft": [{"hex": "a1b2c3", "lat": 36.1, "lon": -115.2,
                                                     "altitude": 5000, "vert_rate": 900}]}
                return r
            raise ConnectionError("refused")

        with mock.patch.object(overhead, "RECEIVER_TYPE", "dump1090"), \
             mock.patch.object(overhead, "_dump1090_good_url", [""]), \
             mock.patch.object(overhead.requests, "get", side_effect=fake_get):
            flights = overhead.fetch_flights()
            self.assertEqual([f.hex_code for f in flights], ["a1b2c3"])
            self.assertEqual(overhead._dump1090_good_url[0], good)
            seen.clear()
            overhead.fetch_flights()
            # fr24feed first, then straight to the remembered dump1090 URL
            self.assertEqual(seen, [overhead.FR24FEED_URL, good])


class AdsbdbRegistration(unittest.TestCase):
    """Tails come from adsbdb now (OpenSky /metadata/aircraft answers 410 Gone)."""

    def setUp(self):
        overhead._reg_miss_until.clear()
        self.addCleanup(overhead._reg_miss_until.clear)

    def _run(self, resp):
        store = {}
        with mock.patch.object(overhead, "_in_backoff", return_value=False), \
             mock.patch.object(overhead, "_cache_db_get_reg", side_effect=lambda h: store.get(h, "")), \
             mock.patch.object(overhead, "_cache_db_set_reg", side_effect=store.__setitem__), \
             mock.patch.object(overhead._session, "get", return_value=resp) as g:
            overhead._try_adsbdb_reg("a1b2c3")
            overhead._try_adsbdb_reg("a1b2c3")
        return store, g.call_count

    def test_found_registration_is_cached(self):
        r = mock.MagicMock(status_code=200)
        r.json.return_value = {"response": {"aircraft": {"registration": "n904sy"}}}
        store, calls = self._run(r)
        self.assertEqual(store, {"a1b2c3": "N904SY"})
        self.assertEqual(calls, 1)        # second call is a cache hit

    def test_unknown_aircraft_is_not_requeried_every_poll(self):
        r = mock.MagicMock(status_code=404)
        r.json.return_value = {"response": "unknown aircraft"}
        store, calls = self._run(r)
        self.assertEqual(store, {})
        self.assertEqual(calls, 1)        # miss remembered for REG_MISS_RETRY_SECS


if __name__ == "__main__":
    unittest.main(verbosity=2)
