import math
import os
import sys
import time

# Allow running standalone: ensure project root is on the path for config imports
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import requests
from threading import Thread, Lock
from time import sleep

try:
    from config import MIN_ALTITUDE
except (ModuleNotFoundError, NameError, ImportError):
    MIN_ALTITUDE = 0  # feet

try:
    from config import MAX_ALTITUDE
except (ModuleNotFoundError, NameError, ImportError):
    MAX_ALTITUDE = 10000  # feet

try:
    from config import ZONE_HOME, LOCATION_HOME
    ZONE_DEFAULT = ZONE_HOME
    LOCATION_DEFAULT = LOCATION_HOME
except (ModuleNotFoundError, NameError, ImportError):
    ZONE_DEFAULT = {"tl_y": 62.61, "tl_x": -13.07, "br_y": 49.71, "br_x": 3.46}
    LOCATION_DEFAULT = [51.509865, -0.118092, 6371]

try:
    from config import RECEIVER_HOST
except (ModuleNotFoundError, NameError, ImportError):
    try:
        from config import DUMP1090_HOST as RECEIVER_HOST
    except (ModuleNotFoundError, NameError, ImportError):
        RECEIVER_HOST = "localhost"

try:
    from config import LOCAL_AIRPORT
except (ModuleNotFoundError, NameError, ImportError):
    LOCAL_AIRPORT = ""

try:
    from config import OPENSKY_CLIENT_ID, OPENSKY_CLIENT_SECRET
except (ModuleNotFoundError, NameError, ImportError):
    OPENSKY_CLIENT_ID = None
    OPENSKY_CLIENT_SECRET = None

try:
    from config import FLIGHTAWARE_API_KEY
except (ModuleNotFoundError, NameError, ImportError):
    FLIGHTAWARE_API_KEY = None

OPENSKY_TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"

# Data source URLs
FR24FEED_URL = f"http://{RECEIVER_HOST}:8754/flights.json"
DUMP1090_URL = f"http://{RECEIVER_HOST}:8080/data/aircraft.json"
AIRPLANESLIVE_URL = "https://api.airplanes.live/v2/hex/{}"
AEROAPI_URL = "https://aeroapi.flightaware.com/aeroapi/flights/{}"
OPENSKY_FLIGHTS_URL = "https://opensky-network.org/api/flights/aircraft"
ADSBDB_CALLSIGN_URL = "https://api.adsbdb.com/v0/callsign/{}"
ADSBDB_AIRCRAFT_URL = "https://api.adsbdb.com/v0/aircraft/{}"

# fr24feed flights.json field indices
# {hex: [hex, lat, lon, heading, alt_ft, speed, squawk, ?, type, reg, timestamp, origin, dest, ?, on_ground, vert_rate, callsign]}
FR24_LAT = 1
FR24_LON = 2
FR24_ALT = 4
FR24_VERT = 15
FR24_CALLSIGN = 16

RATE_LIMIT_DELAY = 1
MAX_FLIGHT_LOOKUP = 5
EARTH_RADIUS_KM = 6371
BLANK_FIELDS = ["", "N/A", "NONE"]

AEROAPI_CACHE_TTL  = 3600  # routes don't change mid-flight
OPENSKY_CACHE_TTL  = 3600  # re-query OpenSky after 1 hour
AIRCRAFT_CACHE_TTL = 86400 # aircraft type is static; 24 hr TTL
CACHE_MAX_SIZE     = 500   # evict oldest entries beyond this

# Module-level lock protecting all shared caches and the OpenSky token
_cache_lock = Lock()
_route_cache    = {}  # hex_code  -> (origin, destination, timestamp)
_aeroapi_cache  = {}  # callsign  -> (origin, destination, timestamp)
_aircraft_cache = {}  # hex_code  -> (type_string, timestamp)
_opensky_token  = {"value": None, "expires_at": 0}


# ── Cache helpers ──────────────────────────────────────────────────────────────

def _prune_cache(cache, ttl):
    """Remove entries older than ttl. Also evict oldest if over CACHE_MAX_SIZE."""
    now = int(time.time())
    stale = [k for k, v in cache.items() if now - v[-1] > ttl]
    for k in stale:
        del cache[k]
    if len(cache) > CACHE_MAX_SIZE:
        oldest = sorted(cache, key=lambda k: cache[k][-1])
        for k in oldest[:len(cache) - CACHE_MAX_SIZE]:
            del cache[k]


def _get_opensky_token():
    """Fetch or return cached OAuth2 Bearer token for OpenSky."""
    now = time.time()
    with _cache_lock:
        if _opensky_token["value"] and now < _opensky_token["expires_at"] - 30:
            return _opensky_token["value"]
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
            alt = alt if isinstance(alt, (int, float)) else 0
            return cls(
                lat=lat,
                lon=lon,
                altitude=alt,
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
        r = requests.get(FR24FEED_URL, timeout=5)
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
        r = requests.get(DUMP1090_URL, timeout=5)
        flights = []
        for ac in r.json().get("aircraft", []):
            f = Flight.from_dump1090(ac)
            if f:
                flights.append(f)
        return flights
    except Exception:
        pass

    return []


def distance_from_flight_to_home(flight, home=LOCATION_DEFAULT):
    def polar_to_cartesian(lat, long, alt):
        DEG2RAD = math.pi / 180
        return [
            alt * math.cos(DEG2RAD * lat) * math.sin(DEG2RAD * long),
            alt * math.sin(DEG2RAD * lat),
            alt * math.cos(DEG2RAD * lat) * math.cos(DEG2RAD * long),
        ]

    def feet_to_meters_plus_earth(altitude_ft):
        return 0.0003048 * altitude_ft + EARTH_RADIUS_KM

    try:
        (x0, y0, z0) = polar_to_cartesian(
            flight.latitude, flight.longitude,
            feet_to_meters_plus_earth(flight.altitude),
        )
        (x1, y1, z1) = polar_to_cartesian(*home)
        return math.sqrt((x1 - x0) ** 2 + (y1 - y0) ** 2 + (z1 - z0) ** 2)
    except AttributeError:
        return 1e6


def in_zone(flight, zone=ZONE_DEFAULT):
    return (
        zone["br_y"] <= flight.latitude <= zone["tl_y"]
        and zone["tl_x"] <= flight.longitude <= zone["br_x"]
    )


# ── Route / type lookup ────────────────────────────────────────────────────────

def get_route(hex_code, callsign, vertical_speed):
    """
    Route lookup priority:
      1. FlightAware AeroAPI (real-time flight plan, paid)
      2. OpenSky by hex (real-time actual departure/arrival)
      3. adsbdb by callsign (static fallback)
      4. LOCAL_AIRPORT heuristic (fill missing end based on climb/descent)
    Returns (origin, destination, source_label).
    """
    origin, destination, source = "", "", ""
    now = int(time.time())

    # 1. FlightAware AeroAPI
    if FLIGHTAWARE_API_KEY and callsign:
        with _cache_lock:
            cached = _aeroapi_cache.get(callsign)
        if cached and now - cached[2] < AEROAPI_CACHE_TTL:
            origin, destination = cached[0], cached[1]
            source = "aeroapi:cached"
        else:
            try:
                r = requests.get(
                    AEROAPI_URL.format(callsign.strip()),
                    headers={"x-apikey": FLIGHTAWARE_API_KEY},
                    timeout=10,
                )
                if r.status_code == 200:
                    flights = r.json().get("flights", [])
                    active = [f for f in flights if not f.get("actual_on")]
                    f = active[0] if active else (flights[0] if flights else None)
                    if f:
                        origin = (f.get("origin") or {}).get("code_iata", "") or ""
                        destination = (f.get("destination") or {}).get("code_iata", "") or ""
                        source = "aeroapi"
                        with _cache_lock:
                            _aeroapi_cache[callsign] = (origin, destination, now)
                            _prune_cache(_aeroapi_cache, AEROAPI_CACHE_TTL)
            except Exception:
                pass

    if origin or destination:
        origin = origin if origin.upper() not in BLANK_FIELDS else ""
        destination = destination if destination.upper() not in BLANK_FIELDS else ""
        return origin, destination, source

    # 2. OpenSky
    if OPENSKY_CLIENT_ID and hex_code:
        with _cache_lock:
            cached = _route_cache.get(hex_code)
        if cached and now - cached[2] < OPENSKY_CACHE_TTL:
            origin, destination = cached[0], cached[1]
            source = "opensky:cached"
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
                    if r.status_code == 200 and r.json():
                        flight = max(r.json(), key=lambda f: f.get("firstSeen", 0))
                        origin = icao_to_iata(flight.get("estDepartureAirport") or "")
                        destination = icao_to_iata(flight.get("estArrivalAirport") or "")
                        source = "opensky"
                        with _cache_lock:
                            _route_cache[hex_code] = (origin, destination, now)
                            _prune_cache(_route_cache, OPENSKY_CACHE_TTL)
                except Exception:
                    pass

    # 3. adsbdb callsign fallback
    if not origin and not destination and callsign:
        try:
            r = requests.get(ADSBDB_CALLSIGN_URL.format(callsign), timeout=5)
            if r.status_code == 200:
                route = r.json().get("response", {}).get("flightroute", {})
                origin = (route.get("origin") or {}).get("iata_code", "") or ""
                destination = (route.get("destination") or {}).get("iata_code", "") or ""
                if origin or destination:
                    source = "adsbdb"
        except Exception:
            pass

    # 4. LOCAL_AIRPORT heuristic
    if LOCAL_AIRPORT:
        departing = vertical_speed > 0
        if departing and not origin:
            origin = LOCAL_AIRPORT
            source = source or "heuristic"
        elif not departing and not destination:
            destination = LOCAL_AIRPORT
            source = source or "heuristic"

    origin = origin if origin.upper() not in BLANK_FIELDS else ""
    destination = destination if destination.upper() not in BLANK_FIELDS else ""
    return origin, destination, source or "none"


def get_aircraft_type(hex_code):
    """
    Aircraft type lookup priority:
      1. airplanes.live (best coverage, has desc field)
      2. adsbdb (fallback)
    Returns (type_string, source_label).
    """
    if not hex_code:
        return "", "none"

    now = int(time.time())
    with _cache_lock:
        cached = _aircraft_cache.get(hex_code)
    # Don't serve empty-string cache entries — retry those
    if cached and cached[0] and now - cached[1] < AIRCRAFT_CACHE_TTL:
        return cached[0], "airplanes.live:cached"

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
                        _aircraft_cache[hex_code] = (plane, now)
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
                    _aircraft_cache[hex_code] = (plane, now)
                    _prune_cache(_aircraft_cache, AIRCRAFT_CACHE_TTL)
                return plane, "adsbdb"
    except Exception:
        pass

    return "", "miss"


# ── Overhead controller ────────────────────────────────────────────────────────

class Overhead:
    def __init__(self):
        self._lock = Lock()
        self._data = []
        self._new_data = False
        self._processing = False

    def grab_data(self):
        # Set processing=True before spawning the thread to prevent duplicate spawns
        with self._lock:
            if self._processing:
                return
            self._processing = True
        Thread(target=self._grab_data, daemon=True).start()

    def _grab_data(self):
        with self._lock:
            self._new_data = False

        data = []
        try:
            all_flights = fetch_flights()
            print(f"[overhead] feed: {len(all_flights)} aircraft", flush=True)
            for f in all_flights:
                in_z = in_zone(f)
                alt_ok = MIN_ALTITUDE < f.altitude < MAX_ALTITUDE
                print(f"[overhead]   {f.callsign or '?':10} alt={f.altitude:6} in_zone={in_z} alt_ok={alt_ok}", flush=True)

            flights = [f for f in all_flights if MIN_ALTITUDE < f.altitude < MAX_ALTITUDE and in_zone(f)]
            flights = sorted(flights, key=distance_from_flight_to_home)

            for flight in flights[:MAX_FLIGHT_LOOKUP]:
                sleep(RATE_LIMIT_DELAY)
                origin, destination, route_src = get_route(flight.hex_code, flight.callsign, flight.vertical_speed)
                plane, type_src = get_aircraft_type(flight.hex_code)
                plane = plane if plane.upper() not in BLANK_FIELDS else ""
                callsign = flight.callsign if flight.callsign.upper() not in BLANK_FIELDS else ""
                print(f"[route:{route_src}] {callsign} {origin}->{destination}", flush=True)
                print(f"[type:{type_src}] {callsign} '{plane}'", flush=True)
                print(f"[overhead]   -> {callsign} plane='{plane}' {origin}->{destination}", flush=True)
                data.append({
                    "plane": plane,
                    "origin": origin,
                    "destination": destination,
                    "vertical_speed": flight.vertical_speed,
                    "altitude": flight.altitude,
                    "callsign": callsign,
                })
        except Exception:
            pass
        finally:
            with self._lock:
                self._new_data = True
                self._processing = False
                self._data = data

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
        sleep(1)
    print(o.data)
