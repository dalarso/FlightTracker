import calendar
import fnmatch
import json
import math
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_PACIFIC = ZoneInfo("America/Los_Angeles")

def _log(msg):
    ts = datetime.now(_PACIFIC).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

# Allow running standalone: ensure project root is on the path for config imports
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import requests
from threading import Thread, Lock

try:
    from config import MIN_ALTITUDE
except (ImportError, NameError):
    MIN_ALTITUDE = 0  # feet

try:
    from config import MAX_ALTITUDE
except (ImportError, NameError):
    MAX_ALTITUDE = 10000  # feet

try:
    from config import ZONE_HOME, LOCATION_HOME
    ZONE_DEFAULT = ZONE_HOME
    LOCATION_DEFAULT = LOCATION_HOME
except (ImportError, NameError):
    ZONE_DEFAULT = {"tl_y": 62.61, "tl_x": -13.07, "br_y": 49.71, "br_x": 3.46}
    LOCATION_DEFAULT = [51.509865, -0.118092, 6371]

try:
    from config import RECEIVER_HOST
except (ImportError, NameError):
    try:
        from config import DUMP1090_HOST as RECEIVER_HOST
    except (ImportError, NameError):
        RECEIVER_HOST = "localhost"

try:
    from config import LOCAL_AIRPORT
except (ImportError, NameError):
    LOCAL_AIRPORT = ""

try:
    from config import OPENSKY_CLIENT_ID, OPENSKY_CLIENT_SECRET
except (ImportError, NameError):
    OPENSKY_CLIENT_ID = None
    OPENSKY_CLIENT_SECRET = None

try:
    from config import FLIGHTAWARE_API_KEY
except (ImportError, NameError):
    FLIGHTAWARE_API_KEY = None

try:
    from config import AIRLABS_API_KEY
except (ImportError, NameError):
    AIRLABS_API_KEY = None


try:
    from config import TIMEZONE
except (ImportError, NameError):
    TIMEZONE = "America/Los_Angeles"

# Update the module-level timezone used by _log() — _log() looks up _PACIFIC
# at call time (not definition time) so this override applies to all future calls.
try:
    _PACIFIC = ZoneInfo(TIMEZONE)
except Exception:
    pass  # keep default if timezone string is invalid

OPENSKY_TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"

# Data source URLs
FR24FEED_URL = f"http://{RECEIVER_HOST}:8754/flights.json"
DUMP1090_URL = f"http://{RECEIVER_HOST}:8080/data/aircraft.json"
AIRPLANESLIVE_URL = "https://api.airplanes.live/v2/hex/{}"
AEROAPI_URL = "https://aeroapi.flightaware.com/aeroapi/flights/{}"
AIRLABS_URL = "https://airlabs.co/api/v9/flight"
OPENSKY_FLIGHTS_URL = "https://opensky-network.org/api/flights/aircraft"
ADSBDB_CALLSIGN_URL = "https://api.adsbdb.com/v0/callsign/{}"
ADSBDB_AIRCRAFT_URL = "https://api.adsbdb.com/v0/aircraft/{}"

# fr24feed flights.json field indices
# {hex: [hex, lat, lon, heading, alt_ft, speed, squawk, ?, type, reg, timestamp, origin, dest, ?, on_ground, vert_rate, callsign]}
FR24_LAT      = 1
FR24_LON      = 2
FR24_ALT      = 4
FR24_VERT     = 15
FR24_CALLSIGN = 16

RATE_LIMIT_DELAY   = 1
FLIGHT_DATA_FILE   = "/tmp/ft_data.json"
APIS_DISABLED_FLAG = "/tmp/ft_apis_disabled"   # combined kill-switch for limited APIs
MAX_FLIGHT_LOOKUP  = 5
EARTH_RADIUS_KM    = 6371
BLANK_FIELDS       = frozenset(["", "N/A", "NONE"])

# Log rotation — plane.log is written via systemd's StandardOutput=append
import pathlib as _pathlib
_LOG_PATH      = _pathlib.Path.home() / "plane.log"
_LOG_MAX_BYTES = 5 * 1024 * 1024   # rotate when file exceeds 5 MB
_LOG_KEEP_BYTES = 2 * 1024 * 1024  # keep the last 2 MB after rotation


def _rotate_log_if_needed():
    """Trim plane.log in-place when it exceeds _LOG_MAX_BYTES."""
    try:
        if _LOG_PATH.stat().st_size <= _LOG_MAX_BYTES:
            return
        content = _LOG_PATH.read_bytes()
        tail = content[-_LOG_KEEP_BYTES:]
        nl = tail.find(b"\n")        # align to a line boundary
        if nl >= 0:
            tail = tail[nl + 1:]
        _LOG_PATH.write_bytes(tail)
        _log(f"[overhead] log rotated — kept {len(tail) // 1024} KB")
    except Exception:
        pass

# Persistent files — stored in project dir so they survive reboots
_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR    = os.path.join(_PROJECT_DIR, "..")
AIRLABS_USAGE_FILE = os.path.join(_DATA_DIR, "airlabs_usage.json")
AEROAPI_USAGE_FILE = os.path.join(_DATA_DIR, "aeroapi_usage.json")
CACHE_FILE         = os.path.join(_DATA_DIR, "ft_cache.json")
OVERRIDES_FILE     = os.path.join(_DATA_DIR, "ft_overrides.json")
AIRLABS_MONTHLY_LIMIT  = 1000        # free tier: 1,000 calls/month
AEROAPI_COST_PER_CALL  = 0.005       # $0.005 per AeroAPI call (informational only)
AIRLABS_RESET_DAY  = 9               # AirLabs billing period resets on the 9th
AEROAPI_RESET_DAY  = 1               # FlightAware credit resets on the 1st

# Trusted local airports — adsbdb results are accepted without real-time
# verification when the origin matches one of these codes.
_LOCAL_AIRPORTS = frozenset(a.upper() for a in [LOCAL_AIRPORT, "VGT"] if a)

_DEG2RAD = math.pi / 180


def _polar_to_cartesian(lat, lon, alt):
    """Convert geographic coordinates + radius to 3-D Cartesian (km)."""
    return (
        alt * math.cos(_DEG2RAD * lat) * math.sin(_DEG2RAD * lon),
        alt * math.sin(_DEG2RAD * lat),
        alt * math.cos(_DEG2RAD * lat) * math.cos(_DEG2RAD * lon),
    )


def _alt_ft_to_earth_radius(altitude_ft):
    """Convert altitude in feet to total radius from Earth's centre (km)."""
    return 0.0003048 * altitude_ft + EARTH_RADIUS_KM


def _haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km between two lat/lon points."""
    dlat = _DEG2RAD * (lat2 - lat1)
    dlon = _DEG2RAD * (lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(_DEG2RAD * lat1) * math.cos(_DEG2RAD * lat2)
         * math.sin(dlon / 2) ** 2)
    return EARTH_RADIUS_KM * 2 * math.asin(math.sqrt(max(0.0, min(1.0, a))))


def _route_plausible(plane_lat, plane_lon, orig_lat, orig_lon, dest_lat, dest_lon):
    """
    Return True if the aircraft's current position is geometrically consistent
    with the given route.  Uses the detour-ratio test from PR #25:

        (dist_plane→origin + dist_plane→dest) / dist_origin→dest < 1.8

    A value ≥ 1.8 means the aircraft is far off the great-circle path —
    a strong signal that the API returned stale or wrong route data.

    Returns True when any coordinate is missing (benefit of the doubt).
    """
    if not all(v is not None for v in (plane_lat, plane_lon,
                                        orig_lat, orig_lon,
                                        dest_lat, dest_lon)):
        return True  # Can't validate — assume plausible

    route_km = _haversine_km(orig_lat, orig_lon, dest_lat, dest_lon)
    if route_km < 80:
        return True  # Short hop — geometry check not reliable at this scale

    d_orig = _haversine_km(plane_lat, plane_lon, orig_lat, orig_lon)
    d_dest = _haversine_km(plane_lat, plane_lon, dest_lat, dest_lon)
    return (d_orig + d_dest) / route_km < 1.8


AEROAPI_CACHE_TTL  = 172800  # paid per call — cache 48 h; scheduled routes are stable
OPENSKY_CACHE_TTL  = 3600    # free/unlimited, hex-keyed (aircraft not callsign) — keep short
ADSBDB_CACHE_TTL   = 3600    # free/unlimited — keep short; fresh data costs nothing
AIRLABS_CACHE_TTL  = 172800  # 1,000 calls/month limit — cache 48 h to protect quota
ROUTE_MISS_TTL     = 300    # negative cache: retry after 5 min when an API has no data
AIRCRAFT_CACHE_TTL = 86400  # aircraft type is static; 24 hr TTL
CACHE_MAX_SIZE     = 500    # evict oldest entries beyond this

# All caches store tuples where the LAST element is always the timestamp,
# making _prune_cache's v[-1] access safe and consistent:
#   _route_cache    : (origin, dest, orig_lat, orig_lon, dest_lat, dest_lon, timestamp)
#   _aeroapi_cache  : (origin, dest, orig_lat, orig_lon, dest_lat, dest_lon, timestamp)
#   _aircraft_cache : (type_string, source_label, timestamp)
# Empty origin+dest with None coords = negative cache entry (API had no data).
_cache_lock     = Lock()
# _route_cache key scheme (three co-existing formats in the same dict):
#   callsign           — adsbdb result, keyed by flight callsign (e.g. "SWA123")
#   hex_code           — OpenSky result, keyed by 6-char ICAO hex (e.g. "a1b2c3")
#   "airlabs:callsign" — AirLabs result, prefixed to avoid collisions with adsbdb entries
_route_cache    = {}
_aeroapi_cache  = {}
_aircraft_cache = {}
_opensky_token  = {"value": None, "expires_at": 0, "fetching": False}

# ── API backoff state ──────────────────────────────────────────────────────────
# Driven by actual HTTP responses, not local counters.
# In-memory only — resets on service restart, which is intentional
# (a fresh restart should retry APIs rather than carry over a stale block).
_api_backoff: dict[str, float] = {}  # api_name -> epoch time to stop backing off

def _in_backoff(api_name: str) -> bool:
    """True if we should skip this API because it recently told us to back off."""
    until = _api_backoff.get(api_name, 0.0)
    return time.time() < until

def _set_backoff(api_name: str, secs: int = 3600) -> None:
    """Record a backoff period after receiving a rate-limit or auth error."""
    _api_backoff[api_name] = time.time() + secs
    _log(f"[{api_name}] backing off for {secs // 60} min")


# ── Cache helpers ──────────────────────────────────────────────────────────────

def _prune_cache(cache, ttl):
    """Remove stale entries and enforce CACHE_MAX_SIZE. Must be called under _cache_lock."""
    now = int(time.time())
    stale = [k for k, v in cache.items() if now - v[-1] > ttl]
    for k in stale:
        del cache[k]
    if len(cache) > CACHE_MAX_SIZE:
        oldest = sorted(cache, key=lambda k: cache[k][-1])
        for k in oldest[:len(cache) - CACHE_MAX_SIZE]:
            del cache[k]


def _save_caches():
    """
    Persist route and aircraft caches to disk so they survive service restarts.
    JSON doesn't support tuples, so entries are serialised as lists and
    converted back to tuples on load.  Uses an atomic tmp→rename write.
    """
    with _cache_lock:
        data = {
            "route":    {k: list(v) for k, v in _route_cache.items()},
            "aeroapi":  {k: list(v) for k, v in _aeroapi_cache.items()},
            "aircraft": {k: list(v) for k, v in _aircraft_cache.items()},
        }
    try:
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, CACHE_FILE)
    except Exception as e:
        _log(f"[cache] WARNING: failed to save persistent cache: {e}")


def _load_caches():
    """
    Load persisted caches from disk on startup.
    Stale entries are discarded; the TTL logic in get_route / get_aircraft_type
    handles freshness correctly because timestamps are embedded in every entry.
    """
    try:
        with open(CACHE_FILE) as f:
            data = json.load(f)
        now = int(time.time())
        loaded = {"route": 0, "aeroapi": 0, "aircraft": 0}
        with _cache_lock:
            for k, v in data.get("route", {}).items():
                if len(v) == 7 and now - v[-1] < max(ADSBDB_CACHE_TTL, AIRLABS_CACHE_TTL, OPENSKY_CACHE_TTL):
                    # Skip miss entries (empty origin + dest) — their short TTL
                    # (ROUTE_MISS_TTL=300s) would have expired anyway, and loading
                    # them back as hour-long hits suppresses retries that should fire.
                    if not v[0] and not v[1]:
                        continue
                    _route_cache[k] = tuple(v)
                    loaded["route"] += 1
            for k, v in data.get("aeroapi", {}).items():
                if len(v) == 7 and now - v[-1] < AEROAPI_CACHE_TTL:
                    _aeroapi_cache[k] = tuple(v)
                    loaded["aeroapi"] += 1
            for k, v in data.get("aircraft", {}).items():
                if len(v) == 3 and now - v[-1] < AIRCRAFT_CACHE_TTL:
                    _aircraft_cache[k] = tuple(v)
                    loaded["aircraft"] += 1
        _log(
            f"[cache] loaded {loaded['route']} route, "
            f"{loaded['aeroapi']} aeroapi, {loaded['aircraft']} aircraft entries"
        )
    except FileNotFoundError:
        pass  # first run — nothing to load
    except Exception as e:
        _log(f"[cache] failed to load persisted cache: {e}")


# Load persisted caches on import so the first poll after a restart is warm.
_load_caches()

# ── Override rules ─────────────────────────────────────────────────────────────
# Loaded on demand with mtime-based invalidation so edits via the web UI take
# effect on the very next poll without needing a service restart.
# _overrides_lock guards the shared globals against concurrent worker threads.
_overrides_lock:  Lock  = Lock()
_overrides_cache: list  = []
_overrides_mtime: float = 0.0

def _load_overrides() -> list:
    """Return the current override rules, reloading from disk when the file changes.
    Thread-safe: callers may be any of the ThreadPoolExecutor worker threads.
    Returns a snapshot copy so callers can iterate without holding the lock.
    """
    global _overrides_cache, _overrides_mtime
    with _overrides_lock:
        try:
            mtime = os.path.getmtime(OVERRIDES_FILE)
            if mtime != _overrides_mtime:
                with open(OVERRIDES_FILE) as f:
                    data = json.load(f)
                if not isinstance(data, list):
                    _log("[override] WARNING: overrides file is not a JSON array — ignoring")
                    data = []
                _overrides_cache = data
                _overrides_mtime = mtime
        except FileNotFoundError:
            _overrides_cache = []
            _overrides_mtime = 0.0
        except Exception as e:
            _log(f"[override] WARNING: failed to load overrides: {e}")
        return list(_overrides_cache)  # snapshot — caller iterates without the lock


def _match_override(callsign: str):
    """
    Return the first matching override rule dict, or None.
    Pattern matching is case-insensitive; * acts as a wildcard anywhere in the pattern.
    """
    if not callsign:
        return None
    cs = callsign.upper()
    for rule in _load_overrides():
        pattern = rule.get("pattern", "").upper()
        if pattern and fnmatch.fnmatch(cs, pattern):
            return rule
    return None


# Module-level thread pool — reused across all poll cycles to avoid per-flight
# thread-creation overhead.  Two lookups (get_route + get_aircraft_type) run in
# parallel per flight; with up to MAX_FLIGHT_LOOKUP flights processed back-to-back,
# size the pool so all concurrent tasks for a full batch can proceed without queuing.
_lookup_executor = ThreadPoolExecutor(max_workers=MAX_FLIGHT_LOOKUP * 2)


def _is_live(src):
    """True when src represents a live (non-cached, non-heuristic) API call."""
    return ":cached" not in src and src not in ("heuristic", "none", "miss", "override")


def _get_opensky_token():
    """Fetch or return cached OAuth2 Bearer token for OpenSky. Thread-safe."""
    now = time.time()
    with _cache_lock:
        if _opensky_token["value"] and now < _opensky_token["expires_at"] - 30:
            return _opensky_token["value"]
        if _opensky_token["fetching"]:
            return None  # another thread is already fetching; skip rather than pile on
        _opensky_token["fetching"] = True

    try:
        r = requests.post(
            OPENSKY_TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": OPENSKY_CLIENT_ID,
                "client_secret": OPENSKY_CLIENT_SECRET,
            },
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            with _cache_lock:
                _opensky_token["value"] = data["access_token"]
                _opensky_token["expires_at"] = now + data.get("expires_in", 300)
            return _opensky_token["value"]
    except Exception:
        pass
    finally:
        with _cache_lock:
            _opensky_token["fetching"] = False

    return None


def icao_to_iata(code):
    """Best-effort ICAO→IATA: strip leading region letter for common prefixes."""
    if not code or len(code) != 4:
        return code
    if code[0] in ("K", "P"):  # US / Alaska
        return code[1:]
    if code[0] == "C":          # Canada: CYYZ → YYZ
        return code[1:]
    return code                 # return 4-char code rather than garble it


# ── Flight model ───────────────────────────────────────────────────────────────

class Flight:
    def __init__(self, lat, lon, altitude, vertical_speed, callsign, hex_code=""):
        self.latitude = lat
        self.longitude = lon
        self.altitude = altitude
        self.vertical_speed = vertical_speed
        self.callsign = callsign
        self.hex_code = hex_code

    @classmethod
    def from_fr24(cls, hex_code, entry):
        try:
            lat = entry[FR24_LAT]
            lon = entry[FR24_LON]
            if not lat or not lon:
                return None
            alt = entry[FR24_ALT]
            return cls(
                lat=lat,
                lon=lon,
                altitude=alt if isinstance(alt, (int, float)) else 0,
                vertical_speed=entry[FR24_VERT] if isinstance(entry[FR24_VERT], (int, float)) else 0,
                callsign=(entry[FR24_CALLSIGN] or "").strip(),
                hex_code=hex_code,
            )
        except (IndexError, TypeError):
            return None

    @classmethod
    def from_dump1090(cls, ac):
        lat = ac.get("lat")
        lon = ac.get("lon")
        if not lat or not lon:
            return None
        alt = ac.get("alt_baro", 0)
        return cls(
            lat=lat,
            lon=lon,
            altitude=alt if isinstance(alt, (int, float)) else 0,
            vertical_speed=ac.get("baro_rate", ac.get("geom_rate", 0)) or 0,
            callsign=(ac.get("flight") or "").strip(),
            hex_code=ac.get("hex", ""),
        )


# ── Data fetching ──────────────────────────────────────────────────────────────

def fetch_flights():
    """Return Flight objects from fr24feed (preferred) or dump1090 (fallback)."""
    try:
        r = requests.get(FR24FEED_URL, timeout=3)   # local LAN — 3 s is plenty
        if r.status_code == 200:
            flights = []
            for hex_code, entry in r.json().items():
                if isinstance(entry, list) and len(entry) > FR24_CALLSIGN:
                    f = Flight.from_fr24(hex_code, entry)
                    if f:
                        flights.append(f)
            return flights
    except Exception:
        pass

    try:
        r = requests.get(DUMP1090_URL, timeout=3)   # local LAN — 3 s is plenty
        if r.status_code == 200:
            flights = []
            for ac in r.json().get("aircraft", []):
                f = Flight.from_dump1090(ac)
                if f:
                    flights.append(f)
            return flights
    except Exception:
        pass

    _log(f"[overhead] receiver unreachable — no data from {RECEIVER_HOST}")
    return []


def distance_from_flight_to_home(flight, home=LOCATION_DEFAULT):
    try:
        x0, y0, z0 = _polar_to_cartesian(
            flight.latitude, flight.longitude,
            _alt_ft_to_earth_radius(flight.altitude),
        )
        x1, y1, z1 = _polar_to_cartesian(*home)
        return math.sqrt((x1 - x0) ** 2 + (y1 - y0) ** 2 + (z1 - z0) ** 2)
    except (AttributeError, TypeError):
        return 1e6


def in_zone(flight, zone=ZONE_DEFAULT):
    return (
        zone["br_y"] <= flight.latitude <= zone["tl_y"]
        and zone["tl_x"] <= flight.longitude <= zone["br_x"]
    )


# ── Route / type lookup ────────────────────────────────────────────────────────

def _billing_period_start(reset_day):
    """
    Return the start date (YYYY-MM-DD) of the current billing period.
    e.g. reset_day=9 on May 15 → '2026-05-09'
         reset_day=9 on May 3  → '2026-04-09'
    """
    today = datetime.now()
    if today.day >= reset_day:
        return today.replace(day=reset_day).strftime("%Y-%m-%d")
    # Before reset day this month — period started last month
    first_of_month = today.replace(day=1)
    last_month = first_of_month - timedelta(days=1)
    # Clamp reset_day to actual days in last month (defensive for reset_day > 28)
    last_month_days = calendar.monthrange(last_month.year, last_month.month)[1]
    return last_month.replace(day=min(reset_day, last_month_days)).strftime("%Y-%m-%d")


def _read_usage(path, reset_day):
    """Return usage dict for the current billing period, resetting if period has rolled over."""
    period = _billing_period_start(reset_day)
    try:
        with open(path) as f:
            data = json.load(f)
        if data.get("period_start") == period:
            return data
    except Exception:
        pass
    return {"period_start": period, "value": 0.0}


def _write_usage(path, data):
    """Atomically write a usage JSON file using a tmp→rename pattern."""
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception as e:
        _log(f"[usage] WARNING: failed to write {os.path.basename(path)}: {e}")


def _cache_entry(orig, dest, olat, olon, dlat, dlon):
    """Build a route cache tuple. Timestamp is always last for _prune_cache."""
    return (orig, dest, olat, olon, dlat, dlon, int(time.time()))


def _airlabs_increment():
    data = _read_usage(AIRLABS_USAGE_FILE, AIRLABS_RESET_DAY)
    data["value"] = data.get("value", 0) + 1
    _write_usage(AIRLABS_USAGE_FILE, data)
    remaining = AIRLABS_MONTHLY_LIMIT - data["value"]
    if remaining <= 50:
        _log(f"[airlabs] WARNING: {int(remaining)} calls remaining this period")


def _aeroapi_increment():
    data = _read_usage(AEROAPI_USAGE_FILE, AEROAPI_RESET_DAY)
    data["value"] = round(data.get("value", 0.0) + AEROAPI_COST_PER_CALL, 4)
    _write_usage(AEROAPI_USAGE_FILE, data)
    _log(f"[aeroapi] period spend so far: ~${data['value']:.3f}")


def get_route(hex_code, callsign, vertical_speed, plane_lat=None, plane_lon=None):
    """
    Route lookup priority:
      0. Override rules — ft_overrides.json; pattern-matched against callsign.
                          Returns immediately, no API calls made at all.
      1. adsbdb       — static historical DB; trusted immediately when origin is a
                        known local airport AND the route passes a geographic
                        plausibility check against the aircraft's current position.
      2. OpenSky      — free, unlimited, real-time by ICAO24 hex.  Trusted without
                        coordinates when a local airport is confirmed by vertical rate:
                        climbing + LAS/VGT origin, or descending + LAS/VGT dest.
                        Through-traffic (both endpoints non-local) falls through to
                        AirLabs since there's no way to verify without coordinates.
      3. AirLabs      — real-time by callsign; now mainly for through-traffic that
                        OpenSky couldn't auto-trust.  Returns airport coordinates,
                        enabling the full plausibility check.  1,000 calls/month free.
      4. adsbdb       — unverified fallback: if every live source returned nothing,
    (unverified)        use the adsbdb result even without local-airport confirmation,
                        rather than calling FlightAware immediately.
      5. FlightAware  — paid last resort; cascades on 402/429.
      6. LOCAL_AIRPORT heuristic — fills one missing endpoint from climb/descent.

    Cache format: (origin, dest, orig_lat, orig_lon, dest_lat, dest_lon, timestamp)
    Negative cache entries have empty origin/dest with None coords — used to
    avoid re-querying APIs within ROUTE_MISS_TTL when they had no data.

    plane_lat/plane_lon: aircraft's current position, used for plausibility checks.
    """
    origin, destination, source = "", "", ""
    now = int(time.time())
    _apis_disabled = os.path.exists(APIS_DISABLED_FLAG)  # evaluate once — two callers below

    # ── 0. Override rules — bypass ALL API lookups for known callsigns ─────────
    # Rules are defined in ft_overrides.json and managed via the web UI.
    # Patterns are case-insensitive; * is a wildcard (e.g. JANET* matches any
    # Janet flight).  An override returns immediately — no adsbdb, OpenSky,
    # AirLabs, or FlightAware calls are made.
    _ov = _match_override(callsign)
    if _ov:
        ov_origin = (_ov.get("origin") or "").strip().upper()
        ov_dest   = (_ov.get("destination") or "").strip().upper()
        _log(
            f"[override] {callsign} matched '{_ov['pattern']}'"
            f" → {ov_origin or '?'}->{ov_dest or '?'}"
            + (f"  ({_ov['note']})" if _ov.get("note") else "")
        )
        return ov_origin, ov_dest, "override"

    # ── 1. adsbdb (static historical DB) ──────────────────────────────────────
    adsbdb_origin = adsbdb_dest = ""
    adsbdb_olat = adsbdb_olon = adsbdb_dlat = adsbdb_dlon = None
    _adsbdb_src = "adsbdb"

    if callsign:
        with _cache_lock:
            cached = _route_cache.get(callsign)
        if cached and now - cached[-1] < (ROUTE_MISS_TTL if not cached[0] else ADSBDB_CACHE_TTL):
            adsbdb_origin, adsbdb_dest = cached[0], cached[1]
            adsbdb_olat, adsbdb_olon = cached[2], cached[3]
            adsbdb_dlat, adsbdb_dlon = cached[4], cached[5]
            _adsbdb_src = "adsbdb:cached"
        else:
            try:
                r = requests.get(ADSBDB_CALLSIGN_URL.format(callsign), timeout=5)
                if r.status_code == 200:
                    fr = (r.json().get("response") or {}).get("flightroute") or {}
                    adsbdb_origin = (fr.get("origin") or {}).get("iata_code", "") or ""
                    adsbdb_dest   = (fr.get("destination") or {}).get("iata_code", "") or ""
                    adsbdb_olat   = (fr.get("origin") or {}).get("latitude")
                    adsbdb_olon   = (fr.get("origin") or {}).get("longitude")
                    adsbdb_dlat   = (fr.get("destination") or {}).get("latitude")
                    adsbdb_dlon   = (fr.get("destination") or {}).get("longitude")
                    # Cache hit or confirmed miss (no flightroute in DB)
                    with _cache_lock:
                        _route_cache[callsign] = _cache_entry(
                            adsbdb_origin, adsbdb_dest,
                            adsbdb_olat, adsbdb_olon, adsbdb_dlat, adsbdb_dlon,
                        )
                        _prune_cache(_route_cache, ADSBDB_CACHE_TTL)
                elif r.status_code == 404:
                    # Callsign not in adsbdb — cache as miss so we don't re-query every poll.
                    # Use ADSBDB_CACHE_TTL (not ROUTE_MISS_TTL) so this prune doesn't evict
                    # valid 1-hour cache entries for other callsigns.  The freshness check on
                    # read still uses ROUTE_MISS_TTL for empty entries (see get_route above).
                    with _cache_lock:
                        _route_cache[callsign] = _cache_entry("", "", None, None, None, None)
                        _prune_cache(_route_cache, ADSBDB_CACHE_TTL)
                # 5xx / unexpected: don't cache — transient error, retry next poll
            except Exception:
                pass

    # Vertical rate — computed once, shared by all trust checks below.
    _climbing   = vertical_speed > 0
    _descending = vertical_speed < 0

    # Trust adsbdb when a local airport appears on either end AND the route can
    # be verified by at least one of:
    #   (a) geometry  — detour-ratio plausibility check (uses airport coordinates)
    #   (b) climbing  — aircraft is climbing while origin is local (just departed)
    #   (c) descending — aircraft is descending while dest is local (arriving)
    # Mirrors the same three-signal logic used for OpenSky below.
    _adsbdb_origin_local = adsbdb_origin.upper() in _LOCAL_AIRPORTS
    _adsbdb_dest_local   = adsbdb_dest.upper()   in _LOCAL_AIRPORTS
    adsbdb_ok = (
        (_adsbdb_origin_local or _adsbdb_dest_local)
        and (adsbdb_origin or adsbdb_dest)
        and (
            _route_plausible(plane_lat, plane_lon,
                             adsbdb_olat, adsbdb_olon,
                             adsbdb_dlat, adsbdb_dlon)
            or (_climbing   and _adsbdb_origin_local)
            or (_descending and _adsbdb_dest_local)
        )
    )
    if adsbdb_ok:
        origin, destination, source = adsbdb_origin, adsbdb_dest, _adsbdb_src

    # ── 2. OpenSky by hex (free, unlimited — queried before AirLabs) ────────────
    # OpenSky doesn't return airport coordinates, so the full geometry plausibility
    # check isn't possible.  However we can trust its result without coordinates
    # when a local airport is confirmed by the aircraft's own vertical rate:
    #
    #   • origin in _LOCAL_AIRPORTS AND climbing   → just departed locally
    #   • dest   in _LOCAL_AIRPORTS AND descending → arriving locally
    #
    # Three independent signals (local airport + correct vertical direction +
    # aircraft physically inside the zone) make this extremely reliable in practice.
    # Pure through-traffic (both endpoints non-local) cannot be auto-trusted this
    # way and falls through to AirLabs, which has airport coordinates for
    # the plausibility check.
    _sky_origin = _sky_dest = ""
    _sky_src = "opensky"
    # OpenSky is free/unlimited — intentionally excluded from the _apis_disabled kill-switch.
    if not (origin and destination) and OPENSKY_CLIENT_ID and hex_code and not _in_backoff("opensky"):
        with _cache_lock:
            cached = _route_cache.get(hex_code)
        if cached and now - cached[-1] < (ROUTE_MISS_TTL if not cached[0] else OPENSKY_CACHE_TTL):
            _sky_origin, _sky_dest = cached[0], cached[1]
            _sky_src = "opensky:cached"
        else:
            token = _get_opensky_token()
            if token:
                try:
                    r = requests.get(
                        OPENSKY_FLIGHTS_URL,
                        params={"icao24": hex_code.lower(), "begin": now - 86400, "end": now},
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=10,
                    )
                    if r.status_code == 429:
                        _set_backoff("opensky", secs=3600)
                    elif r.status_code in (401, 403):
                        _set_backoff("opensky", secs=86400)
                        _log(f"[opensky] auth error ({r.status_code}) — check credentials")
                    else:
                        sky_data = r.json() if r.status_code == 200 else []
                        if sky_data:
                            fl = max(sky_data, key=lambda f: f.get("firstSeen", 0))
                            _sky_origin = icao_to_iata(fl.get("estDepartureAirport") or "")
                            _sky_dest   = icao_to_iata(fl.get("estArrivalAirport") or "")
                        # Cache result regardless — suppresses repeated API calls within TTL
                        with _cache_lock:
                            _route_cache[hex_code] = _cache_entry(
                                _sky_origin, _sky_dest,
                                None, None, None, None,  # OpenSky returns no airport coords
                            )
                            _prune_cache(_route_cache, OPENSKY_CACHE_TTL)
                except Exception:
                    pass

        if _sky_origin or _sky_dest:
            # Trust when a local airport is confirmed by the aircraft's vertical rate.
            _sky_origin_local = _sky_origin.upper() in _LOCAL_AIRPORTS
            _sky_dest_local   = _sky_dest.upper()   in _LOCAL_AIRPORTS
            _opensky_trusted  = (
                (_sky_origin_local and _climbing)   or   # departing locally → reliable
                (_sky_dest_local   and _descending)      # arriving locally  → reliable
            )
            if _opensky_trusted:
                if not origin:
                    origin = _sky_origin
                if not destination:
                    destination = _sky_dest
                source = source or _sky_src
            else:
                # Through-traffic: can't verify without coords — fall through to AirLabs.
                # Result is already cached so OpenSky won't be re-queried this cycle.
                _log(
                    f"[opensky] {_sky_origin}->{_sky_dest} not auto-trusted for {callsign}"
                    f" (through-traffic, vs={vertical_speed}) — trying AirLabs"
                )

    # ── 3. AirLabs (real-time, 1,000 calls/month — now mainly through-traffic) ──
    # Only called when OpenSky's result wasn't auto-trusted or returned nothing.
    # Returns airport coordinates, enabling the full plausibility check.
    _need_airlabs = not (origin and destination)
    al_origin = al_dest = ""
    al_olat = al_olon = al_dlat = al_dlon = None
    _al_src = "airlabs"

    if _need_airlabs and AIRLABS_API_KEY and callsign and not _apis_disabled:
        with _cache_lock:
            cached = _route_cache.get(f"airlabs:{callsign}")
        if cached and now - cached[-1] < (ROUTE_MISS_TTL if not cached[0] else AIRLABS_CACHE_TTL):
            al_origin, al_dest = cached[0], cached[1]
            al_olat, al_olon = cached[2], cached[3]
            al_dlat, al_dlon = cached[4], cached[5]
            _al_src = "airlabs:cached"
        elif not _in_backoff("airlabs"):
            try:
                r = requests.get(
                    AIRLABS_URL,
                    params={"flight_icao": callsign, "api_key": AIRLABS_API_KEY},
                    timeout=5,
                )
                if r.status_code == 429:
                    _set_backoff("airlabs", secs=3600)
                elif r.status_code in (401, 403):
                    _set_backoff("airlabs", secs=86400)
                    _log(f"[airlabs] auth error ({r.status_code}) — check AIRLABS_API_KEY")
                elif r.status_code == 200:
                    _airlabs_increment()  # informational counter only
                    resp = r.json().get("response") or {}
                    al_origin = resp.get("dep_iata", "") or ""
                    al_dest   = resp.get("arr_iata", "") or ""
                    al_olat   = resp.get("dep_lat")
                    al_olon   = resp.get("dep_lng")
                    al_dlat   = resp.get("arr_lat")
                    al_dlon   = resp.get("arr_lng")
                    # Cache result (even empty — negative cache suppresses retries)
                    with _cache_lock:
                        _route_cache[f"airlabs:{callsign}"] = _cache_entry(
                            al_origin, al_dest,
                            al_olat, al_olon, al_dlat, al_dlon,
                        )
                        _prune_cache(_route_cache, AIRLABS_CACHE_TTL)
                    if al_origin or al_dest:
                        _al_src = "airlabs"
                else:
                    # Unexpected status (e.g. 404, 500) — negatively cache for
                    # ROUTE_MISS_TTL to prevent repeated quota-burning calls.
                    # Use AIRLABS_CACHE_TTL for prune so we don't evict valid
                    # 1-hour entries for other callsigns already in _route_cache.
                    _log(f"[airlabs] unexpected status {r.status_code} for {callsign} — negative caching")
                    with _cache_lock:
                        _route_cache[f"airlabs:{callsign}"] = _cache_entry(
                            "", "", None, None, None, None,
                        )
                        _prune_cache(_route_cache, AIRLABS_CACHE_TTL)
            except Exception:
                pass
        else:
            _log("[airlabs] in backoff — skipping")

    # AirLabs returns coordinates — apply the full plausibility check.
    if al_origin or al_dest:
        al_plausible = _route_plausible(plane_lat, plane_lon,
                                         al_olat, al_olon, al_dlat, al_dlon)
        if al_plausible:
            origin      = al_origin if al_origin else origin
            destination = al_dest   if al_dest   else destination
            source      = _al_src
        else:
            _log(f"[airlabs] implausible route {al_origin}->{al_dest} rejected for {callsign}")

    # If every live source came up empty, use the adsbdb result as a bridge —
    # even though it didn't pass the local-airport trust check — rather than
    # calling FlightAware immediately.
    if not origin and not destination and (adsbdb_origin or adsbdb_dest):
        origin      = adsbdb_origin
        destination = adsbdb_dest
        source      = _adsbdb_src + ":unverified"

    # ── 4. FlightAware AeroAPI (paid — last resort, capped at monthly limit) ───
    if not (origin and destination) and FLIGHTAWARE_API_KEY and callsign and not _apis_disabled:
        with _cache_lock:
            cached = _aeroapi_cache.get(callsign)
        if cached and now - cached[-1] < (ROUTE_MISS_TTL if not cached[0] else AEROAPI_CACHE_TTL):
            if not origin:
                origin = cached[0]
            if not destination:
                destination = cached[1]
            source = source or "aeroapi:cached"
        elif not _in_backoff("aeroapi"):
            try:
                r = requests.get(
                    AEROAPI_URL.format(callsign.strip()),
                    headers={"x-apikey": FLIGHTAWARE_API_KEY},
                    timeout=10,
                )
                if r.status_code == 429:
                    _set_backoff("aeroapi", secs=3600)
                elif r.status_code == 402:
                    # Payment required — over budget or no credit remaining
                    _set_backoff("aeroapi", secs=86400)
                    _log("[aeroapi] 402 payment required — over credit limit")
                elif r.status_code in (401, 403):
                    _set_backoff("aeroapi", secs=86400)
                    _log(f"[aeroapi] auth error ({r.status_code}) — check FLIGHTAWARE_API_KEY")
                elif r.status_code == 200:
                    flights = r.json().get("flights", [])
                    # Prefer an en-route flight; fall back to most recent
                    active = [f for f in flights if not f.get("actual_on")]
                    f = active[0] if active else (flights[0] if flights else None)
                    fa_origin = fa_dest = ""
                    fa_olat = fa_olon = fa_dlat = fa_dlon = None
                    if f:
                        fa_origin = (f.get("origin") or {}).get("code_iata", "") or ""
                        fa_dest   = (f.get("destination") or {}).get("code_iata", "") or ""
                        fa_olat   = (f.get("origin") or {}).get("latitude")
                        fa_olon   = (f.get("origin") or {}).get("longitude")
                        fa_dlat   = (f.get("destination") or {}).get("latitude")
                        fa_dlon   = (f.get("destination") or {}).get("longitude")
                    _aeroapi_increment()  # informational counter + spend tracking
                    with _cache_lock:
                        _aeroapi_cache[callsign] = _cache_entry(
                            fa_origin, fa_dest,
                            fa_olat, fa_olon, fa_dlat, fa_dlon,
                        )
                        _prune_cache(_aeroapi_cache, AEROAPI_CACHE_TTL)
                    if fa_origin or fa_dest:
                        fa_plausible = _route_plausible(plane_lat, plane_lon,
                                                         fa_olat, fa_olon,
                                                         fa_dlat, fa_dlon)
                        if fa_plausible:
                            if not origin:
                                origin = fa_origin
                                source = "aeroapi"
                            if not destination:
                                destination = fa_dest
                                source = source or "aeroapi"
                        else:
                            _log(f"[aeroapi] implausible route {fa_origin}->{fa_dest} rejected for {callsign}")
                else:
                    # Unexpected status (4xx/5xx other than explicitly handled codes) —
                    # negatively cache for ROUTE_MISS_TTL to prevent per-poll retries.
                    _log(f"[aeroapi] unexpected status {r.status_code} for {callsign} — negative caching")
                    with _cache_lock:
                        _aeroapi_cache[callsign] = _cache_entry("", "", None, None, None, None)
                        _prune_cache(_aeroapi_cache, AEROAPI_CACHE_TTL)
            except Exception:
                pass
        # else: in backoff — already logged when backoff was set

    # ── 5. LOCAL_AIRPORT heuristic ─────────────────────────────────────────────
    if LOCAL_AIRPORT:
        departing = vertical_speed > 0
        if departing and not origin:
            origin = LOCAL_AIRPORT
            source = source or "heuristic"
        elif not departing and not destination:
            destination = LOCAL_AIRPORT
            source = source or "heuristic"

    origin      = origin      if origin.upper()      not in BLANK_FIELDS else ""
    destination = destination if destination.upper() not in BLANK_FIELDS else ""
    return origin, destination, source or "none"


def get_aircraft_type(hex_code):
    """
    Aircraft type lookup priority:
      1. airplanes.live (best coverage, has desc field)
      2. adsbdb (fallback)
    Cache tuple: (type_string, source_label, timestamp) — timestamp last for _prune_cache.
    Returns (type_string, source_label).
    """
    if not hex_code:
        return "", "none"

    now = int(time.time())
    with _cache_lock:
        cached = _aircraft_cache.get(hex_code)
    # cached[0]=type, cached[1]=source, cached[2]=timestamp
    if cached:
        age = now - cached[2]
        if cached[0] and age < AIRCRAFT_CACHE_TTL:
            return cached[0], f"{cached[1]}:cached"   # known type, still fresh
        # No miss TTL — always retry: get_aircraft_type is only called for in-zone
        # flights, so every retry is justified.  A successful hit ends retries for 24h.

    # 1. airplanes.live
    try:
        r = requests.get(AIRPLANESLIVE_URL.format(hex_code.upper()), timeout=5)
        if r.status_code == 200:
            ac_list = r.json().get("ac", [])
            if ac_list:
                ac = ac_list[0]
                plane = ac.get("desc", "") or ac.get("t", "") or ""
                if plane:
                    with _cache_lock:
                        _aircraft_cache[hex_code] = (plane, "airplanes.live", now)
                        _prune_cache(_aircraft_cache, AIRCRAFT_CACHE_TTL)
                    return plane, "airplanes.live"
    except Exception:
        pass

    # 2. adsbdb
    try:
        r = requests.get(ADSBDB_AIRCRAFT_URL.format(hex_code), timeout=5)
        if r.status_code == 200:
            ac = r.json().get("response", {}).get("aircraft", {})
            manufacturer = ac.get("manufacturer", "") or ""
            type_name = ac.get("type", "") or ""
            plane = f"{manufacturer} {type_name}".strip()
            if plane:
                with _cache_lock:
                    _aircraft_cache[hex_code] = (plane, "adsbdb", now)
                    _prune_cache(_aircraft_cache, AIRCRAFT_CACHE_TTL)
                return plane, "adsbdb"
    except Exception:
        pass

    # No miss caching — retry every poll cycle until the type resolves.
    # Both APIs are free/unlimited and this function is only called for in-zone
    # flights, so the extra calls are acceptable and the data actually matters.
    return "", "miss"


# ── Overhead controller ────────────────────────────────────────────────────────

class Overhead:
    def __init__(self):
        self._lock = Lock()
        self._data = []
        self._new_data = False
        self._processing = False

    def grab_data(self):
        """Spawn a background fetch. No-ops if one is already in progress."""
        with self._lock:
            if self._processing:
                return
            self._processing = True
        Thread(target=self._grab_data, daemon=True).start()

    def _grab_data(self):
        with self._lock:
            self._new_data = False

        data = []
        success = False
        try:
            all_flights = fetch_flights()
            in_zone_flights = [
                f for f in all_flights
                if MIN_ALTITUDE < f.altitude < MAX_ALTITUDE and in_zone(f)
            ]
            out_count = len(all_flights) - len(in_zone_flights)
            _log(
                f"[overhead] feed: {len(all_flights)} aircraft "
                f"({len(in_zone_flights)} in zone, {out_count} out)"
            )
            for f in in_zone_flights:
                _log(
                    f"[overhead]   {f.callsign or '?':10} "
                    f"alt={f.altitude:6} in_zone=True alt_ok=True"
                )

            flights = sorted(in_zone_flights, key=distance_from_flight_to_home)

            prev_was_live = False
            for i, flight in enumerate(flights[:MAX_FLIGHT_LOOKUP]):
                # Only rate-limit when the *previous* flight made a live API call.
                # Cached lookups need no courtesy delay.
                if i > 0 and prev_was_live:
                    time.sleep(RATE_LIMIT_DELAY)

                # ── route + aircraft-type lookups run in parallel ──────────────
                # _lookup_executor is module-level — no per-flight thread spin-up cost.
                _route_fut = _lookup_executor.submit(
                    get_route,
                    flight.hex_code, flight.callsign, flight.vertical_speed,
                    flight.latitude, flight.longitude,
                )
                _type_fut = _lookup_executor.submit(get_aircraft_type, flight.hex_code)
                origin, destination, route_src = _route_fut.result()
                plane, type_src              = _type_fut.result()

                plane    = plane    if plane.upper()    not in BLANK_FIELDS else ""
                callsign = flight.callsign if flight.callsign.upper() not in BLANK_FIELDS else ""

                _log(f"[route:{route_src}] [type:{type_src}] {callsign} {origin}->{destination} '{plane}'")
                _log(f"[overhead]   -> {callsign} plane='{plane}' {origin}->{destination}")

                prev_was_live = _is_live(route_src) or _is_live(type_src)

                data.append({
                    "plane": plane,
                    "origin": origin,
                    "destination": destination,
                    "vertical_speed": flight.vertical_speed,
                    "altitude": flight.altitude,
                    "callsign": callsign,
                })

            success = True

        except Exception:
            _log(f"[overhead] error in _grab_data:\n{traceback.format_exc()}")
        finally:
            if success:
                # Only overwrite shared state and ft_data.json when the full poll
                # completed without an exception.  A mid-poll crash can leave `data`
                # partial or empty — never blank the display with a bad result.
                try:
                    tmp = FLIGHT_DATA_FILE + ".tmp"
                    with open(tmp, "w") as f:
                        json.dump({"ts": int(time.time()), "flights": data}, f)
                    os.replace(tmp, FLIGHT_DATA_FILE)
                except Exception:
                    pass

                # Persist caches only when in-zone flights were processed.
                # Route and type lookups only happen for in-zone flights, so
                # an empty result means the caches are unchanged from the last
                # write — skipping the write reduces unnecessary SD card I/O.
                if data:
                    _save_caches()

                with self._lock:
                    self._data = data

            _rotate_log_if_needed()

            with self._lock:
                self._new_data = True
                self._processing = False

    @property
    def new_data(self):
        with self._lock:
            return self._new_data

    @property
    def processing(self):
        with self._lock:
            return self._processing

    @property
    def data(self):
        with self._lock:
            self._new_data = False
            return self._data

    @property
    def data_is_empty(self):
        with self._lock:
            return len(self._data) == 0


# ── Standalone test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    o = Overhead()
    o.grab_data()
    while not o.new_data:
        print("processing...")
        time.sleep(1)
    print(o.data)
