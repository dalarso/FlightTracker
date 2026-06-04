import calendar
import fnmatch
import json
import logging
import math
import os
import re
import sqlite3
import sys
import time
import traceback
import warnings
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
from collections import namedtuple
from utilities.geometry import _haversine_km, _route_plausible  # pure geometry, extracted
from threading import Thread, Lock, local
_cache_bypass = local()  # set .on=True to make cache READS miss (test-flight no-cache mode)

# FlightRadar24 — unofficial public API; used for GA (N-number) route lookups.
# The FlightRadarAPI package must be installed: pip install FlightRadarAPI
try:
    from FlightRadarAPI import FlightRadar24API as _FlightRadar24API
    _FR24_AVAILABLE = True
    # FR24 sometimes sends a gzip Content-Encoding header on an already-decompressed
    # body; the library logs a WARNING but recovers (falls back to raw bytes).  Silence
    # that cosmetic noise — keep ERROR+ so genuine library failures still surface.
    logging.getLogger("FlightRadarAPI").setLevel(logging.ERROR)
except ImportError:
    _FR24_AVAILABLE = False
    _FlightRadar24API = None

_fr24_instance = None
_fr24_lock = Lock()

def _get_fr24_api():
    """Return a shared FlightRadar24API instance, lazy-initialised on first call."""
    global _fr24_instance
    if not _FR24_AVAILABLE:
        return None
    if _fr24_instance is None:
        with _fr24_lock:
            if _fr24_instance is None:
                try:
                    _fr24_instance = _FlightRadar24API()
                except Exception:
                    return None
    return _fr24_instance


def _fr24_alt_int(flight_obj) -> int:
    """Safely convert FR24 Flight.altitude to int.

    The FR24 library occasionally returns the string "ground" (or other
    non-numeric values) instead of 0 for landed aircraft.  Using int() directly
    raises ValueError; this helper returns 0 in that case.
    """
    try:
        return int(getattr(flight_obj, 'altitude', 0) or 0)
    except (ValueError, TypeError):
        return 0

try:
    from config import MIN_ALTITUDE
except Exception:
    MIN_ALTITUDE = 0  # feet

try:
    from config import MAX_ALTITUDE
except Exception:
    MAX_ALTITUDE = 10000  # feet

try:
    from config import ZONE_HOME, LOCATION_HOME
    ZONE_DEFAULT = ZONE_HOME
    LOCATION_DEFAULT = LOCATION_HOME
except Exception:
    ZONE_DEFAULT = {"tl_y": 62.61, "tl_x": -13.07, "br_y": 49.71, "br_x": 3.46}
    LOCATION_DEFAULT = [51.509865, -0.118092, 6371]

try:
    from config import RECEIVER_HOST
except Exception:
    try:
        from config import DUMP1090_HOST as RECEIVER_HOST
    except Exception:
        RECEIVER_HOST = "localhost"

try:
    from config import RECEIVER_TYPE
except Exception:
    RECEIVER_TYPE = "dump1090"  # "dump1090" | "vrs"

try:
    from config import LOCAL_AIRPORTS
except Exception:
    LOCAL_AIRPORTS = ""

# Backward-compat: if only the old single-value key exists, use it as a seed.
try:
    from config import LOCAL_AIRPORT as _LOCAL_AIRPORT_LEGACY
except Exception:
    _LOCAL_AIRPORT_LEGACY = ""

try:
    from config import OPENSKY_CLIENT_ID, OPENSKY_CLIENT_SECRET
except Exception:
    OPENSKY_CLIENT_ID = None
    OPENSKY_CLIENT_SECRET = None

try:
    from config import FLIGHTAWARE_API_KEY
except Exception:
    FLIGHTAWARE_API_KEY = None

try:
    from config import AIRLABS_API_KEY
except Exception:
    AIRLABS_API_KEY = None

try:
    from config import AIRLABS_API_KEY_2
except Exception:
    AIRLABS_API_KEY_2 = None


try:
    from config import TIMEZONE
except Exception:
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
# Virtual Radar Server — default HTTP port is 8080; override RECEIVER_HOST to include
# a non-standard port, e.g. RECEIVER_HOST = "192.168.1.50:8090"
VRS_URL      = f"http://{RECEIVER_HOST}:8080/VirtualRadar/AircraftList.json"
AIRPLANESLIVE_URL = "https://api.airplanes.live/v2/hex/{}"
AEROAPI_URL = "https://aeroapi.flightaware.com/aeroapi/flights/{}"
AIRLABS_URL = "https://airlabs.co/api/v9/flight"
OPENSKY_FLIGHTS_URL = "https://opensky-network.org/api/flights/aircraft"
ADSBDB_CALLSIGN_URL  = "https://api.adsbdb.com/v0/callsign/{}"
ADSBDB_AIRCRAFT_URL  = "https://api.adsbdb.com/v0/aircraft/{}"
OPENSKY_AIRCRAFT_URL = "https://opensky-network.org/api/metadata/aircraft/icao/{}"  # public, no token

# fr24feed flights.json field indices
# {hex: [hex, lat, lon, heading, alt_ft, speed, squawk, ?, type, reg, timestamp, origin, dest, ?, on_ground, vert_rate, callsign]}
FR24_LAT      = 1
FR24_LON      = 2
FR24_ALT      = 4
FR24_REG      = 9   # aircraft registration / tail number
FR24_VERT     = 15
FR24_CALLSIGN = 16

RATE_LIMIT_DELAY   = 1
FLIGHT_DATA_FILE   = "/tmp/ft_data.json"
APIS_DISABLED_FLAG    = "/tmp/ft_apis_disabled"   # combined kill-switch (AirLabs + AeroAPI)
ADSBDB_DISABLED_FLAG  = "/tmp/ft_adsbdb_disabled"
OPENSKY_DISABLED_FLAG = "/tmp/ft_opensky_disabled"
AIRLABS_DISABLED_FLAG  = "/tmp/ft_airlabs_disabled"
AIRLABS2_DISABLED_FLAG = "/tmp/ft_airlabs2_disabled"
AEROAPI_DISABLED_FLAG  = "/tmp/ft_aeroapi_disabled"
FR24_DISABLED_FLAG     = "/tmp/ft_fr24_disabled"
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
AIRLABS_USAGE_FILE  = os.path.join(_DATA_DIR, "airlabs_usage.json")
AIRLABS2_USAGE_FILE = os.path.join(_DATA_DIR, "airlabs2_usage.json")
AEROAPI_USAGE_FILE = os.path.join(_DATA_DIR, "aeroapi_usage.json")
OVERRIDES_FILE     = os.path.join(_DATA_DIR, "ft_overrides.json")
TEST_DISPLAY_FILE  = "/tmp/ft_test_display.json"   # written by run_test_lookup(); read by _grab_data()
AEROAPI_COST_PER_CALL  = 0.005       # $0.005 per AeroAPI call (platform rate — not user-configurable)

# Billing tracking constants — can be overridden in config.py (managed via web config page)
try:
    from config import AIRLABS_MONTHLY_LIMIT
except Exception:
    AIRLABS_MONTHLY_LIMIT = 1000     # free tier: 1,000 calls/month

try:
    from config import AIRLABS_RESET_DAY
except Exception:
    AIRLABS_RESET_DAY = 9            # AirLabs billing period resets on the 9th

try:
    from config import AIRLABS2_MONTHLY_LIMIT
except Exception:
    AIRLABS2_MONTHLY_LIMIT = 1000    # free tier: 1,000 calls/month

try:
    from config import AIRLABS2_RESET_DAY
except Exception:
    AIRLABS2_RESET_DAY = 9           # AirLabs 2 billing period resets on the 9th

try:
    from config import AEROAPI_RESET_DAY
except Exception:
    AEROAPI_RESET_DAY = 1            # FlightAware credit resets on the 1st

# Local airports — used for journey/zone display features.
# LOCAL_AIRPORTS is a comma-separated string (e.g. "LAS,VGT,HSH") set in config.py.
# Falls back gracefully to the old single-value LOCAL_AIRPORT key for compatibility.
_raw_airports = LOCAL_AIRPORTS if LOCAL_AIRPORTS else _LOCAL_AIRPORT_LEGACY
_LOCAL_AIRPORTS = frozenset(a.strip().upper() for a in _raw_airports.split(",") if a.strip())

# ── Airport city names (IATA code → city) for log display only ───────────────
_AIRPORT_CITIES: dict[str, str] = {
    # Nevada / local
    "LAS": "Las Vegas",       "VGT": "N. Las Vegas",    "HSH": "Henderson",
    "BLD": "Boulder City",
    # US West
    "LAX": "Los Angeles",     "SFO": "San Francisco",   "SAN": "San Diego",
    "PHX": "Phoenix",         "TUS": "Tucson",           "ABQ": "Albuquerque",
    "ELP": "El Paso",         "SMF": "Sacramento",       "OAK": "Oakland",
    "SJC": "San Jose",        "BUR": "Burbank",          "LGB": "Long Beach",
    "ONT": "Ontario CA",      "PSP": "Palm Springs",     "SBA": "Santa Barbara",
    "FAT": "Fresno",          "RNO": "Reno",             "SLC": "Salt Lake City",
    "SEA": "Seattle",         "PDX": "Portland",         "BOI": "Boise",
    "GEG": "Spokane",         "MFR": "Medford",          "EUG": "Eugene",
    # US Mountain / Central
    "COS": "Colorado Springs","GJT": "Grand Junction",   "ASE": "Aspen",
    "EGE": "Eagle/Vail",      "HDN": "Hayden/Steamboat", "PUB": "Pueblo",
    "DEN": "Denver",          "DFW": "Dallas/Fort Worth","DAL": "Dallas",
    "AUS": "Austin",          "SAT": "San Antonio",      "HOU": "Houston",
    "IAH": "Houston",         "MSP": "Minneapolis",      "MCI": "Kansas City",
    "STL": "St. Louis",       "MKE": "Milwaukee",        "ORD": "Chicago",
    "MDW": "Chicago",         "DSM": "Des Moines",       "OMA": "Omaha",
    "MSN": "Madison",         "GRR": "Grand Rapids",     "FSD": "Sioux Falls",
    "RAP": "Rapid City",      "BIS": "Bismarck",         "GFK": "Grand Forks",
    "FAR": "Fargo",           "ABR": "Aberdeen SD",      "LNK": "Lincoln NE",
    # US Mountain West
    "BIL": "Billings",        "MSO": "Missoula",         "GTF": "Great Falls",
    "BZN": "Bozeman",         "IDA": "Idaho Falls",      "PIH": "Pocatello",
    "JAC": "Jackson Hole",    "SUN": "Sun Valley",       "TWF": "Twin Falls",
    # US East
    "ATL": "Atlanta",         "CLT": "Charlotte",        "MEM": "Memphis",
    "BNA": "Nashville",       "DTW": "Detroit",          "CLE": "Cleveland",
    "CMH": "Columbus",        "PIT": "Pittsburgh",       "IND": "Indianapolis",
    "CVG": "Cincinnati",      "PHL": "Philadelphia",     "EWR": "Newark",
    "JFK": "New York",        "LGA": "New York",         "BOS": "Boston",
    "BWI": "Baltimore",       "IAD": "Washington DC",    "DCA": "Washington DC",
    "RDU": "Raleigh-Durham",  "ORF": "Norfolk",          "RIC": "Richmond",
    "SYR": "Syracuse",        "ALB": "Albany",           "BDL": "Hartford",
    "PVD": "Providence",      "BUF": "Buffalo",          "ROC": "Rochester",
    "RFD": "Rockford",        "MLI": "Moline",           "CID": "Cedar Rapids",
    # US Southeast / South
    "MCO": "Orlando",         "TPA": "Tampa",            "MIA": "Miami",
    "FLL": "Fort Lauderdale", "RSW": "Fort Myers",       "PBI": "West Palm Beach",
    "SRQ": "Sarasota",        "JAX": "Jacksonville",     "SAV": "Savannah",
    "CHS": "Charleston",      "GSO": "Greensboro",       "GSP": "Greenville",
    "MSY": "New Orleans",     "BHM": "Birmingham",       "MOB": "Mobile",
    "LIT": "Little Rock",     "OKC": "Oklahoma City",    "TUL": "Tulsa",
    "XNA": "Fayetteville",    "TYS": "Knoxville",        "HSV": "Huntsville",
    "VPS": "Destin/Ft Walton","PNS": "Pensacola",        "MLB": "Melbourne FL",
    "ECP": "Panama City FL",  "PIE": "St. Petersburg",   "SFB": "Sanford FL",
    "DAB": "Daytona Beach",   "AGS": "Augusta",          "CAE": "Columbia SC",
    # US Hawaii / Alaska
    "HNL": "Honolulu",        "OGG": "Maui",             "KOA": "Kona",
    "LIH": "Lihue",           "ITO": "Hilo",
    "ANC": "Anchorage",       "FAI": "Fairbanks",        "JNU": "Juneau",
    "KTN": "Ketchikan",       "SIT": "Sitka",
    # Canada
    "YYZ": "Toronto",         "YVR": "Vancouver",        "YUL": "Montreal",
    "YYC": "Calgary",         "YEG": "Edmonton",         "YWG": "Winnipeg",
    "YOW": "Ottawa",          "YHZ": "Halifax",          "YYJ": "Victoria",
    "YKA": "Kamloops",        "YLW": "Kelowna",
    # Mexico
    "MEX": "Mexico City",     "CUN": "Cancún",           "GDL": "Guadalajara",
    "MTY": "Monterrey",       "SJD": "Los Cabos",        "PVR": "Puerto Vallarta",
    "MZT": "Mazatlán",        "ZIH": "Ixtapa",           "HMO": "Hermosillo",
    "BJX": "León",            "TLC": "Toluca",           "OAX": "Oaxaca",
    # Caribbean
    "NAS": "Nassau",          "GCM": "Grand Cayman",     "MBJ": "Montego Bay",
    "SJU": "San Juan",        "STT": "St. Thomas",
    # Europe
    "LHR": "London",          "LGW": "London Gatwick",   "MAN": "Manchester",
    "CDG": "Paris",           "ORY": "Paris Orly",       "FRA": "Frankfurt",
    "AMS": "Amsterdam",       "ZRH": "Zürich",           "MAD": "Madrid",
    "BCN": "Barcelona",       "FCO": "Rome",             "MXP": "Milan",
    "VIE": "Vienna",          "DUB": "Dublin",           "CPH": "Copenhagen",
    "ARN": "Stockholm",       "OSL": "Oslo",             "HEL": "Helsinki",
    "LIS": "Lisbon",          "ATH": "Athens",           "IST": "Istanbul",
    "BRU": "Brussels",        "MUC": "Munich",           "DUS": "Düsseldorf",
    "HAM": "Hamburg",         "BER": "Berlin",           "PRG": "Prague",
    "WAW": "Warsaw",          "BUD": "Budapest",         "GVA": "Geneva",
    # Middle East
    "DXB": "Dubai",           "DOH": "Doha",             "AUH": "Abu Dhabi",
    "TLV": "Tel Aviv",        "AMM": "Amman",
    # Asia / Pacific
    "ICN": "Seoul",           "GMP": "Seoul Gimpo",      "NRT": "Tokyo",
    "HND": "Tokyo",           "KIX": "Osaka",            "PEK": "Beijing",
    "PKX": "Beijing",         "PVG": "Shanghai",         "SHA": "Shanghai",
    "HKG": "Hong Kong",       "TPE": "Taipei",           "SIN": "Singapore",
    "KUL": "Kuala Lumpur",    "BKK": "Bangkok",          "HAN": "Hanoi",
    "SGN": "Ho Chi Minh City","CGK": "Jakarta",          "MNL": "Manila",
    "CEB": "Cebu",
    # Australia / New Zealand
    "SYD": "Sydney",          "MEL": "Melbourne",        "BNE": "Brisbane",
    "PER": "Perth",           "AKL": "Auckland",         "CHC": "Christchurch",
    # Latin America
    "BOG": "Bogotá",          "MDE": "Medellín",         "CLO": "Cali",
    "PTY": "Panama City",     "SJO": "San José",         "GUA": "Guatemala City",
    "SAP": "San Pedro Sula",  "LIM": "Lima",             "UIO": "Quito",
    "GIG": "Rio de Janeiro",  "GRU": "São Paulo",        "BSB": "Brasília",
    "SCL": "Santiago",        "EZE": "Buenos Aires",     "MVD": "Montevideo",
    "ASU": "Asunción",        "VVI": "Santa Cruz",
}

def _clean_iata(code):
    """Return a real 3-letter IATA airport code, else '' (rendered as '?').
    OpenSky and other feeds sometimes report FAA local identifiers ('NV98'), 4-char ICAO,
    or junk — none are IATA airports the hierarchy can place, and a non-3-char code doesn't
    fit the display.  Applied at get_route's boundary only; raw codes stay in debug logs."""
    code = (code or "").strip().upper()
    return code if (len(code) == 3 and code.isalpha()) else ""


def _route_display(origin: str, dest: str) -> str:
    """
    Build a log-friendly route string with city names where known.
    'LAS->MKE (Las Vegas to Milwaukee)'
    Falls back gracefully: 'LAS->XYZ (Las Vegas)' or 'LAS->MKE' if neither known.
    """
    o = (origin or "?").upper()
    d = (dest   or "?").upper()
    route  = f"{o}->{d}"
    o_city = _AIRPORT_CITIES.get(o)
    d_city = _AIRPORT_CITIES.get(d)
    if o_city and d_city:
        return f"{route} ({o_city} to {d_city})"
    if o_city:
        return f"{route} ({o_city})"
    if d_city:
        return f"{route} (to {d_city})"
    return route

# ── Airline display names (ICAO 3-letter prefix → human-readable name) ────────
_AIRLINE_NAMES: dict[str, str] = {
    # US majors
    "AAL": "American Airlines",   "DAL": "Delta Air Lines",
    "UAL": "United Airlines",     "SWA": "Southwest Airlines",
    "ASA": "Alaska Airlines",     "JBU": "JetBlue Airways",
    "NKS": "Spirit Airlines",     "FFT": "Frontier Airlines",
    "SCX": "Sun Country Airlines","AAY": "Allegiant Air",
    "HAL": "Hawaiian Airlines",   "VRD": "Virgin America",
    # US ULCCs / leisure
    "MXY": "Breeze Airways",      "VXP": "Avelo Airlines",
    "JSX": "JSX",
    # Canadian regional / leisure
    "ROU": "Air Canada Rouge",
    # Charter / private aviation
    "EJA": "NetJets",              "LXJ": "Flexjet",             "JRE": "flyExclusive",
    "TIV": "Thrive Aviation",      "CXK": "ATP Flight School",
    "TWY": "Solarius Aviation",    "KAI": "KaiserAir",
    # Las Vegas special (government contractor — Groom Lake / Area 51 shuttle)
    "JAN": "Janet Airlines",
    # US cargo
    "FDX": "FedEx Express",       "UPS": "UPS Airlines",
    "GTI": "Atlas Air",           "ABX": "ABX Air",
    "ASN": "Amazon Air",          "PAC": "Polar Air Cargo",
    "CKS": "Kalitta Air",         "WGN": "Western Global Airlines",
    "NCR": "Northern Air Cargo",  "SOU": "Southern Air",
    "DHK": "DHL Aviation",        "AGX": "Amerijet International",
    # US charters / military contract
    "OAE": "Omni Air International",
    # German leisure (Lufthansa Group)
    "OCN": "Discover Airlines",
    # Canadian
    "ACA": "Air Canada",          "WJA": "WestJet",
    "POE": "Porter Airlines",     "FLE": "Flair Airlines",
    "SWG": "Sunwing Airlines",
    # Mexican
    "AMX": "Aeroméxico",          "VOI": "Volaris",
    "VIV": "VivaAerobus",
    # European
    "BAW": "British Airways",     "VIR": "Virgin Atlantic",
    "AFR": "Air France",          "DLH": "Lufthansa",
    "KLM": "KLM",                 "UAE": "Emirates",
    "QTR": "Qatar Airways",       "SIA": "Singapore Airlines",
    "EIN": "Aer Lingus",          "IBE": "Iberia",
    "CFG": "Condor",              "EDW": "Edelweiss Air",
    "THY": "Turkish Airlines",    "ETD": "Etihad Airways",
    "SWR": "Swiss Int'l",         "AUA": "Austrian Airlines",
    "NAX": "Norwegian",           "EZY": "easyJet",
    "RYR": "Ryanair",             "TAP": "TAP Air Portugal",
    "FIN": "Finnair",             "BEL": "Brussels Airlines",
    # Asian / Pacific
    "KAL": "Korean Air",          "QFA": "Qantas",
    "ANA": "All Nippon Airways",  "JAL": "Japan Airlines",
    "CPA": "Cathay Pacific",      "EVA": "EVA Air",
    "CCA": "Air China",           "CSN": "China Southern",
    "ANZ": "Air New Zealand",
    # Latin American
    "CMP": "Copa Airlines",       "AVA": "Avianca",
    # Regional/commuter
    "SKW": "SkyWest Airlines",    "ENY": "Envoy Air",
    "RPA": "Republic Airways",    "QXE": "Horizon Air",
    "ASH": "Mesa Airlines",       "PDT": "Piedmont Airlines",
    "JIA": "PSA Airlines",        "UCA": "CommutAir",
    "CPZ": "Comair",              "MTN": "Mountain Air Cargo",
    "FLG": "Frontier (charter)",
}

def _airline_display(callsign: str) -> str:
    """Return 'SWA123 (Southwest Airlines)' if prefix known, else just callsign."""
    if not callsign or len(callsign) < 3:
        return callsign
    name = _AIRLINE_NAMES.get(callsign[:3].upper())
    return f"{callsign} ({name})" if name else callsign

# ── ICAO aircraft type code → human-readable translation ──────────────────────
_AIRCRAFT_TYPE_MAP: dict[str, str] = {
    # Boeing narrow-body
    "B731": "Boeing 737-100",   "B732": "Boeing 737-200",
    "B733": "Boeing 737-300",   "B734": "Boeing 737-400",
    "B735": "Boeing 737-500",   "B736": "Boeing 737-600",
    "B737": "Boeing 737-700",   "B738": "Boeing 737-800",
    "B739": "Boeing 737-900",   "B37M": "Boeing 737 MAX 7",
    "B38M": "Boeing 737 MAX 8", "B39M": "Boeing 737 MAX 9",
    "B3XM": "Boeing 737 MAX 10",
    # Boeing wide-body
    "B744": "Boeing 747-400",   "B748": "Boeing 747-8",
    "B752": "Boeing 757-200",   "B753": "Boeing 757-300",
    "B762": "Boeing 767-200",   "B763": "Boeing 767-300",
    "B764": "Boeing 767-400",
    "B772": "Boeing 777-200",   "B77L": "Boeing 777-200LR",
    "B773": "Boeing 777-300",   "B77W": "Boeing 777-300ER",
    "B778": "Boeing 777X-8",    "B779": "Boeing 777X-9",
    "B788": "Boeing 787-8",     "B789": "Boeing 787-9",
    "B78X": "Boeing 787-10",
    # Airbus narrow-body
    "A318": "Airbus A318",      "A319": "Airbus A319",
    "A320": "Airbus A320",      "A321": "Airbus A321",
    "A19N": "Airbus A319neo",   "A20N": "Airbus A320neo",
    "A21N": "Airbus A321neo",   "A22N": "Airbus A321XLR",
    # Airbus wide-body
    "A332": "Airbus A330-200",  "A333": "Airbus A330-300",
    "A338": "Airbus A330-800neo","A339": "Airbus A330-900neo",
    "A342": "Airbus A340-200",  "A343": "Airbus A340-300",
    "A345": "Airbus A340-500",  "A346": "Airbus A340-600",
    "A359": "Airbus A350-900",  "A35K": "Airbus A350-1000",
    "A388": "Airbus A380",
    # Embraer
    "E170": "Embraer E170",     "E175": "Embraer E175",
    "E190": "Embraer E190",     "E195": "Embraer E195",
    "E75L": "Embraer E175-E2",  "E290": "Embraer E190-E2",
    "E295": "Embraer E195-E2",
    # Bombardier CRJ
    "CRJ1": "Bombardier CRJ-100","CRJ2": "Bombardier CRJ-200",
    "CRJ7": "Bombardier CRJ-700","CRJ9": "Bombardier CRJ-900",
    "CRJX": "Bombardier CRJ-1000",
    # Dash 8 / Q-series
    "DH8A": "Dash 8-100",       "DH8B": "Dash 8-200",
    "DH8C": "Dash 8-300",       "DH8D": "Dash 8-400",
    "DHC8": "Dash 8",
    # ATR
    "AT43": "ATR 42-300",       "AT45": "ATR 42-500",
    "AT72": "ATR 72-200",       "AT75": "ATR 72-500",
    "AT76": "ATR 72-600",
    # Business jets — Cessna Citation
    "C25A": "Citation CJ2",     "C25B": "Citation CJ3",
    "C25C": "Citation CJ4",     "C510": "Citation Mustang",
    "C525": "Citation CJ1",     "C550": "Citation II",
    "C56X": "Citation Excel",   "C560": "Citation V",
    "C680": "Citation Sovereign","C750": "Citation X",
    # Business jets — Gulfstream
    "GLF4": "Gulfstream IV",    "GLF5": "Gulfstream V",
    "G150": "Gulfstream G150",  "G280": "Gulfstream G280",
    "G550": "Gulfstream G550",  "G650": "Gulfstream G650",
    "G700": "Gulfstream G700",
    # Business jets — Bombardier Global/Challenger
    "CL30": "Bombardier Challenger 300",
    "CL35": "Bombardier Challenger 350",
    "CL60": "Bombardier Challenger 600",
    "GL5T": "Bombardier Global 5000",
    "GLEX": "Bombardier Global Express",
    "GL7T": "Bombardier Global 7500",
    # Business jets — Learjet
    "LJ35": "Learjet 35",       "LJ45": "Learjet 45",
    "LJ60": "Learjet 60",       "LJ75": "Learjet 75",
    # Business jets — Dassault Falcon
    "F2TH": "Falcon 2000",      "FA7X": "Falcon 7X",
    "F900": "Falcon 900",       "F8EX": "Falcon 8X",
    # Legacy MD / Boeing
    "MD11": "MD-11",            "MD80": "MD-80",
    "MD82": "MD-82",            "MD83": "MD-83",
    "MD88": "MD-88",            "MD90": "MD-90",
    # GA — Cessna piston
    "C172": "Cessna 172",       "C182": "Cessna 182",
    "C208": "Cessna Caravan",   "C210": "Cessna 210",
    # GA — Piper
    "P28A": "Piper PA-28",      "P28B": "Piper PA-28",
    "PA34": "Piper Seneca",     "PA46": "Piper Malibu/Meridian",
    # GA — Beechcraft
    "BE20": "King Air 200",     "BE35": "Bonanza 35",
    "BE36": "Bonanza 36",       "BE58": "Baron 58",
    "BE9L": "King Air 90",      "BE99": "Beechcraft 1900",
    # Helicopters
    "B06":  "Bell 206",         "B407": "Bell 407",
    "B412": "Bell 412",         "EC35": "EC135",
    "EC45": "EC145",            "H125": "Airbus H125",
    "R22":  "Robinson R22",     "R44":  "Robinson R44",
    "S76":  "Sikorsky S-76",
}

def _translate_type(type_str: str) -> str:
    """Map a raw ICAO type code (e.g. 'B738') to a readable name if known."""
    if not type_str:
        return type_str
    return _AIRCRAFT_TYPE_MAP.get(type_str.strip().upper(), type_str)


def _vrs_airport_to_iata(vrs_str: str) -> str:
    """Extract an IATA code from a VRS airport string.

    VRS encodes airports as "{code} {full name}", e.g.:
      "KLAS Las Vegas Harry Reid Intl"
      "KLAX Los Angeles Intl"
      "EGLL London Heathrow"
    For US (K...) and Canadian (C...) ICAO codes, drop the leading letter to
    get the 3-letter IATA code.  Everything else is used as-is (already IATA
    or an ICAO code the display layer can show verbatim).
    Returns "" when the input is empty or unparseable.
    """
    if not vrs_str:
        return ""
    code = vrs_str.split()[0].strip().upper()
    if len(code) == 4 and code[0] in ("K", "C") and code[1:].isalpha():
        return code[1:]   # KLAS → LAS, CYVR → YVR
    return code           # return full code — EGLL, LFPG, etc. stay intact

# ── Paid-API skip rules — GA registrations and known non-commercial prefixes ──
# N-numbers (US civil registrations) never have filed routes in paid APIs.
# Similarly, known military/government operator prefixes are skipped to avoid
# burning quota on callsigns that will always return empty.
_N_NUMBER_RE = re.compile(r"^N\d", re.IGNORECASE)

_SKIP_PAID_PREFIXES = frozenset([
    # US military air mobility / special missions
    "RCH",   # REACH  — Air Mobility Command airlift
    "PAT",   # PATRIOT — AMC passenger service
    "SAM",   # Special Air Mission (VIP)
    "HKY",   # Husky  — various military
    # DOE / government special use
    "DOE",
    # JANET callsign prefix (classified flights to Groom Lake)
    "JAN",
])

def _skip_paid_apis(callsign: str) -> bool:
    """
    Return True when paid APIs (AirLabs/AeroAPI) should be skipped for this
    callsign.  Two cases:
      • N-number registrations (GA flying VFR — no filed route exists)
      • Known non-commercial ICAO prefixes (military, government)
    Free APIs (adsbdb, OpenSky) are still tried — they may have historical data.
    """
    if not callsign:
        return False
    if _N_NUMBER_RE.match(callsign):
        return True
    if len(callsign) >= 3 and callsign[:3].upper() in _SKIP_PAID_PREFIXES:
        return True
    return False

# ── Flight statistics ──────────────────────────────────────────────────────────
DB_FILE      = os.path.join(_DATA_DIR, "ft_flights.db")
_stats_lock  = Lock()
_stats_seen_today: set = set()   # (date, callsign) already counted — survives restart via JSON
_stats_last_date: str  = ""
_db_conn:    sqlite3.Connection | None = None
_cache_conn: sqlite3.Connection | None = None

def _init_db() -> None:
    """
    Open (or create) the SQLite flight-sightings database and apply the schema.
    WAL mode + NORMAL synchronous — greatly reduces SD-card write amplification
    vs. DELETE journal mode while still being crash-safe.
    _db_conn  : main connection for sightings + api_calls (serialised via _stats_lock).
    _cache_conn: separate connection for the cache table (serialised via _cache_lock),
                 allowing concurrent cache access without blocking sightings writes.
    """
    global _db_conn, _cache_conn
    try:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sightings (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                seen_at      TEXT NOT NULL,
                date         TEXT NOT NULL,
                callsign     TEXT NOT NULL,
                registration TEXT NOT NULL DEFAULT '',
                origin       TEXT NOT NULL DEFAULT '',
                destination  TEXT NOT NULL DEFAULT '',
                aircraft     TEXT NOT NULL DEFAULT '',
                route_source TEXT NOT NULL DEFAULT '',
                airline      TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_seen_cs
            ON sightings(date, callsign)
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_date         ON sightings(date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_callsign     ON sightings(callsign)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_registration ON sightings(registration)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_origin       ON sightings(origin)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_destination  ON sightings(destination)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS api_calls (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                date     TEXT NOT NULL,
                api_name TEXT NOT NULL,
                count    INTEGER NOT NULL DEFAULT 0,
                UNIQUE(date, api_name)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ac_date ON api_calls(date)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cache (
                key        TEXT    NOT NULL,
                cache_type TEXT    NOT NULL,
                origin     TEXT    NOT NULL DEFAULT '',
                dest       TEXT    NOT NULL DEFAULT '',
                olat       REAL,
                olon       REAL,
                dlat       REAL,
                dlon       REAL,
                value      TEXT    NOT NULL DEFAULT '',
                source     TEXT    NOT NULL DEFAULT '',
                expires_at INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (key, cache_type)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache(expires_at)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS overrides (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                position    INTEGER NOT NULL DEFAULT 0,
                pattern     TEXT    NOT NULL,
                origin      TEXT    NOT NULL DEFAULT '',
                destination TEXT    NOT NULL DEFAULT '',
                display     TEXT    NOT NULL DEFAULT '',
                plane       TEXT    NOT NULL DEFAULT '',
                note        TEXT    NOT NULL DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS overrides_meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT '0'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS free_api_checks (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                seen_at    TEXT NOT NULL,
                date       TEXT NOT NULL,
                callsign   TEXT NOT NULL,
                free_route TEXT NOT NULL DEFAULT '',
                paid_route TEXT NOT NULL DEFAULT '',
                matched    INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_fac_date ON free_api_checks(date)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ga_free_api_checks (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                seen_at      TEXT NOT NULL,
                date         TEXT NOT NULL,
                registration TEXT NOT NULL DEFAULT '',
                callsign     TEXT NOT NULL DEFAULT '',
                free_api     TEXT NOT NULL DEFAULT '',
                free_route   TEXT NOT NULL DEFAULT '',
                fr24_route   TEXT NOT NULL DEFAULT '',
                matched      INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ga_fac_date ON ga_free_api_checks(date)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ga_fac_reg  ON ga_free_api_checks(registration)"
        )
        # One-time migration from ft_overrides.json → DB (runs only when table is empty)
        existing = conn.execute("SELECT COUNT(*) FROM overrides").fetchone()[0]
        if existing == 0 and os.path.exists(OVERRIDES_FILE):
            try:
                with open(OVERRIDES_FILE) as _f:
                    _rules = json.load(_f)
                if isinstance(_rules, list):
                    for _pos, _rule in enumerate(_rules):
                        conn.execute(
                            "INSERT INTO overrides "
                            "(position, pattern, origin, destination, display, plane, note) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (
                                _pos,
                                _rule.get("pattern",     "").strip().upper(),
                                _rule.get("origin",      "").strip().upper(),
                                _rule.get("destination", "").strip().upper(),
                                _rule.get("display",     "").strip(),
                                _rule.get("plane",       "").strip(),
                                _rule.get("note",        "").strip(),
                            ),
                        )
                    conn.execute(
                        "INSERT OR REPLACE INTO overrides_meta (key, value) VALUES ('version', '1')"
                    )
                    _log(f"[overhead] migrated {len(_rules)} override rules from JSON to DB")
            except Exception:
                # Clear any partially-inserted rows so the next boot can retry cleanly
                conn.execute("DELETE FROM overrides")
                conn.commit()  # commit the rollback DELETE immediately
                _log("[overhead] WARNING: override migration failed — " + traceback.format_exc())
        conn.commit()
        _db_conn = conn

        # Separate connection for cache reads/writes — serialised via _cache_lock.
        # busy_timeout handles the rare case of both connections writing simultaneously.
        cconn = sqlite3.connect(DB_FILE, check_same_thread=False)
        cconn.execute("PRAGMA journal_mode=WAL")
        cconn.execute("PRAGMA synchronous=NORMAL")
        cconn.execute("PRAGMA busy_timeout=5000")
        _cache_conn = cconn

        _log("[overhead] SQLite DB ready — " + DB_FILE)

    except Exception:
        _log("[overhead] WARNING: could not open SQLite DB — " + traceback.format_exc())


# ── SQLite cache helpers ───────────────────────────────────────────────────────
# All helpers use _cache_conn (separate from _db_conn) and are serialised via
# _cache_lock.  Never raise — cache failures must never crash the poll loop.

def _cache_db_get_route(key: str, cache_type: str):
    """Return (origin, dest, olat, olon, dlat, dlon, source) for a fresh cache entry, or None."""
    if getattr(_cache_bypass, "on", False) or _cache_conn is None:
        return None
    try:
        with _cache_lock:
            row = _cache_conn.execute(
                """SELECT origin, dest, olat, olon, dlat, dlon, COALESCE(source, '')
                   FROM cache WHERE key=? AND cache_type=? AND expires_at>?""",
                (key, cache_type, int(time.time())),
            ).fetchone()
        return row
    except Exception:
        return None


def _cache_db_set_route(key: str, cache_type: str,
                         origin: str, dest: str,
                         olat, olon, dlat, dlon,
                         expires_at: int,
                         source: str = "") -> None:
    """Upsert a route/aeroapi cache entry, recording the originating API source."""
    if _cache_conn is None:
        return
    try:
        with _cache_lock:
            _cache_conn.execute(
                """INSERT OR REPLACE INTO cache
                   (key, cache_type, origin, dest, olat, olon, dlat, dlon, expires_at, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (key, cache_type, origin or '', dest or '',
                 olat, olon, dlat, dlon, expires_at, source or ''),
            )
            _cache_conn.commit()
    except Exception as _e:
        # A swallowed write here was the leading suspect for stale resolved entries
        # (Issue B): if the resolved-cache INSERT throws, the route silently re-resolves
        # every flyover.  Surface it instead of failing invisibly so a genuine DB-write
        # problem shows up in the log rather than as a phantom cache miss.
        _log(f"[cache] write failed for {cache_type}:{key} — {type(_e).__name__}: {_e}")


def _cache_db_delete_route(key: str, cache_type: str) -> None:
    """Delete a single route cache entry by key and type."""
    if _cache_conn is None:
        return
    try:
        with _cache_lock:
            _cache_conn.execute(
                "DELETE FROM cache WHERE key=? AND cache_type=?",
                (key, cache_type),
            )
            _cache_conn.commit()
    except Exception:
        pass


# Expired cache rows are invisible to reads (every SELECT filters `expires_at>?`), but
# nothing deletes them, so the table grows monotonically on the SD card — bloating the
# file and the B-trees.  A low-frequency sweep reclaims the space using idx_cache_expires.
_CACHE_PURGE_INTERVAL_SECS = 6 * 3600
_last_cache_purge = 0.0

# Retention windows (days) for the historical stats tables, applied by the same sweep.
# The two accuracy cross-check tables are internal diagnostics → bounded by default so they
# can't grow forever on the SD card.  `sightings` is user-facing history → kept forever
# unless the user opts into a window (SIGHTINGS_RETENTION_DAYS > 0).
try:
    from config import API_CHECK_RETENTION_DAYS as _API_CHECK_RETENTION_DAYS
    _API_CHECK_RETENTION_DAYS = int(_API_CHECK_RETENTION_DAYS)
except (ImportError, NameError, ValueError, TypeError):
    _API_CHECK_RETENTION_DAYS = 90
try:
    from config import SIGHTINGS_RETENTION_DAYS as _SIGHTINGS_RETENTION_DAYS
    _SIGHTINGS_RETENTION_DAYS = int(_SIGHTINGS_RETENTION_DAYS)
except (ImportError, NameError, ValueError, TypeError):
    _SIGHTINGS_RETENTION_DAYS = 0   # 0 = keep all sighting history (default)


def _purge_expired_cache() -> None:
    """Delete expired rows from the cache table.  Behaviour-preserving (reads already
    ignore expired rows); this only reclaims space + keeps indexes lean.  Time-gated so
    it runs ~4x/day regardless of caller, and wrapped so it can never crash the poll loop."""
    global _last_cache_purge
    _now = time.time()
    if (_now - _last_cache_purge) < _CACHE_PURGE_INTERVAL_SECS or _cache_conn is None:
        return
    _last_cache_purge = _now   # set before the delete so concurrent callers skip
    try:
        with _cache_lock:
            cur = _cache_conn.execute("DELETE FROM cache WHERE expires_at < ?", (int(_now),))
            _cache_conn.commit()
            _n = cur.rowcount
        if _n and _n > 0:
            _log(f"[cache] purged {_n} expired rows")
    except Exception as _e:
        _log(f"[cache] purge failed — {type(_e).__name__}: {_e}")

    # Retention sweep for the historical stats tables (same low-frequency gate, but the
    # stats connection/lock).  Bounds the internal accuracy tables by default; only touches
    # the user-facing `sightings` history when SIGHTINGS_RETENTION_DAYS is explicitly set.
    if _db_conn is not None and (_API_CHECK_RETENTION_DAYS > 0 or _SIGHTINGS_RETENTION_DAYS > 0):
        try:
            with _stats_lock:
                if _API_CHECK_RETENTION_DAYS > 0:
                    _arg = f"-{_API_CHECK_RETENTION_DAYS} days"
                    _db_conn.execute("DELETE FROM free_api_checks    WHERE date < date('now', ?)", (_arg,))
                    _db_conn.execute("DELETE FROM ga_free_api_checks WHERE date < date('now', ?)", (_arg,))
                if _SIGHTINGS_RETENTION_DAYS > 0:
                    _db_conn.execute("DELETE FROM sightings WHERE date < date('now', ?)",
                                     (f"-{_SIGHTINGS_RETENTION_DAYS} days",))
                _db_conn.commit()
        except Exception as _e:
            _log(f"[stats] retention sweep failed — {type(_e).__name__}: {_e}")


def _cache_db_get_aircraft(hex_code: str):
    """Return (type_str, source) for a fresh aircraft cache entry, or None."""
    if getattr(_cache_bypass, "on", False) or _cache_conn is None:
        return None
    try:
        with _cache_lock:
            row = _cache_conn.execute(
                """SELECT value, source FROM cache
                   WHERE key=? AND cache_type='aircraft' AND expires_at>?""",
                (hex_code, int(time.time())),
            ).fetchone()
        return row
    except Exception:
        return None


def _cache_db_set_aircraft(hex_code: str, type_str: str,
                            source: str, ttl: int) -> None:
    """Upsert an aircraft type cache entry."""
    if _cache_conn is None:
        return
    try:
        expires = int(time.time()) + ttl
        with _cache_lock:
            _cache_conn.execute(
                """INSERT OR REPLACE INTO cache
                   (key, cache_type, value, source, expires_at)
                   VALUES (?, 'aircraft', ?, ?, ?)""",
                (hex_code, type_str or '', source or '', expires),
            )
            _cache_conn.commit()
    except Exception:
        pass


def _cache_db_get_reg(hex_code: str) -> str:
    """Return registration string for this hex code, or '' if not cached / expired."""
    if getattr(_cache_bypass, "on", False) or _cache_conn is None:
        return ''
    try:
        with _cache_lock:
            row = _cache_conn.execute(
                "SELECT value FROM cache WHERE key=? AND cache_type='reg' AND expires_at>?",
                (hex_code, int(time.time())),
            ).fetchone()
        return row[0] if row else ''
    except Exception:
        return ''


REG_CACHE_TTL = 365 * 24 * 3600  # 1 year — hex codes are permanent but evict if unseen

def _cache_db_set_reg(hex_code: str, reg: str) -> None:
    """Upsert a registration entry (1-year TTL — evicts aircraft not seen in a year)."""
    if _cache_conn is None:
        return
    try:
        with _cache_lock:
            _cache_conn.execute(
                """INSERT OR REPLACE INTO cache
                   (key, cache_type, value, expires_at)
                   VALUES (?, 'reg', ?, ?)""",
                (hex_code, reg, int(time.time()) + REG_CACHE_TTL),
            )
            _cache_conn.commit()
    except Exception:
        pass


def _cache_db_check_paid_miss(callsign: str) -> bool:
    """Return True if callsign has a fresh paid-API-miss entry."""
    if _cache_conn is None or getattr(_cache_bypass, "on", False):
        return False
    try:
        with _cache_lock:
            row = _cache_conn.execute(
                """SELECT 1 FROM cache
                   WHERE key=? AND cache_type='paid_miss' AND expires_at>?""",
                (callsign, int(time.time())),
            ).fetchone()
        return row is not None
    except Exception:
        return False


def _cache_db_set_paid_miss(callsign: str) -> None:
    """Record that both paid APIs returned empty for callsign; prune stale entries."""
    if _cache_conn is None:
        return
    try:
        expires = int(time.time()) + ROUTE_PAID_MISS_TTL
        with _cache_lock:
            _cache_conn.execute(
                """INSERT OR REPLACE INTO cache
                   (key, cache_type, expires_at)
                   VALUES (?, 'paid_miss', ?)""",
                (callsign, expires),
            )
            _cache_conn.execute(
                "DELETE FROM cache WHERE cache_type='paid_miss' AND expires_at<?",
                (int(time.time()),),
            )
            _cache_conn.commit()
    except Exception:
        pass


def _load_stats_seen_today() -> None:
    """
    Restore today's seen-callsign set at startup so restarts don't double-count.
    Source: sightings table in ft_flights.db (authoritative dedup store).
    """
    global _stats_last_date
    today = datetime.now(_PACIFIC).strftime("%Y-%m-%d")
    _stats_last_date = today
    if _db_conn is not None:
        try:
            with _stats_lock:
                rows = _db_conn.execute(
                    "SELECT callsign FROM sightings WHERE date = ?", (today,)
                ).fetchall()
            for row in rows:
                _stats_seen_today.add((today, row[0]))
            if _stats_seen_today:
                _log(f"[overhead] restored {len(_stats_seen_today)} today's sightings from DB")
        except Exception:
            pass


def _record_flight_stat(callsign: str, plane_type: str, origin: str, dest: str,
                        registration: str = "", route_src: str = "") -> None:
    """
    Record an overhead sighting in the SQLite sightings table.
    Each (date, callsign) is counted at most once — deduplicated in memory via
    _stats_seen_today (rebuilt from the DB on startup).
    Never raises — stats must never crash the main poll loop.
    """
    global _stats_last_date
    if not callsign:
        return
    today  = datetime.now(_PACIFIC).strftime("%Y-%m-%d")
    key    = (today, callsign)
    prefix = callsign[:3].upper() if len(callsign) >= 3 else "???"
    try:
        with _stats_lock:
            # Day rollover — prune stale in-memory entries and log yesterday's summary.
            if today != _stats_last_date:
                _stats_seen_today.clear()
                if _stats_last_date and _db_conn is not None:
                    try:
                        _prev_total = _db_conn.execute(
                            "SELECT COUNT(*) FROM sightings WHERE date=?",
                            (_stats_last_date,),
                        ).fetchone()[0]
                        _prev_rows = _db_conn.execute(
                            "SELECT airline, COUNT(*) cnt FROM sightings "
                            "WHERE date=? GROUP BY airline ORDER BY cnt DESC LIMIT 5",
                            (_stats_last_date,),
                        ).fetchall()
                        _top_str = ", ".join(f"{r[0]}×{r[1]}" for r in _prev_rows)
                        _log(f"[stats] {_stats_last_date}: {_prev_total} flights — top: {_top_str}")
                    except Exception:
                        pass
                _stats_last_date = today

            # Deduplicate in memory (fast path — avoids a DB read for repeat polls).
            if key in _stats_seen_today:
                # Don't double-count, but still fill in fields that were empty on
                # the original insert (registration often arrives on a later poll
                # once airplanes.live has populated the cache).
                if _db_conn is not None and (registration or plane_type):
                    try:
                        _db_conn.execute(
                            """UPDATE sightings SET
                                registration = CASE WHEN registration='' AND ?!='' THEN ? ELSE registration END,
                                aircraft     = CASE WHEN (aircraft='' OR ?='override') AND ?!='' THEN ? ELSE aircraft END,
                                route_source = CASE WHEN ?='override' THEN ? ELSE route_source END
                               WHERE date=? AND callsign=?""",
                            (registration, registration,
                             route_src, plane_type, plane_type,
                             route_src, route_src,
                             today, callsign),
                        )
                        _db_conn.commit()
                    except Exception as exc:
                        _log(f"[overhead] DB enrich failed for {callsign}: {exc}")
                return
            # Persist to SQLite — INSERT OR IGNORE honours the UNIQUE INDEX on
            # (date, callsign) so replays on re-import are harmless.
            # Add to in-memory set AFTER a successful write so that a transient
            # DB failure doesn't silently suppress retries for the rest of the
            # process lifetime.  If the DB is unavailable we still track in
            # memory to prevent double-counting during this session.
            if _db_conn is not None:
                try:
                    seen_at = datetime.now(_PACIFIC).strftime("%Y-%m-%d %H:%M:%S")
                    _db_conn.execute(
                        """
                        INSERT OR IGNORE INTO sightings
                            (seen_at, date, callsign, registration,
                             origin, destination, aircraft, route_source, airline)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (seen_at, today, callsign, registration,
                         origin or "", dest or "", plane_type or "",
                         route_src or "", prefix),
                    )
                    _db_conn.commit()
                    _stats_seen_today.add(key)
                except Exception as exc:
                    _log(f"[overhead] DB insert failed for {callsign}: {exc}")
                    # key NOT added to memory → next poll will retry the INSERT
            else:
                # No DB — track in memory only to avoid double-counting this session
                _stats_seen_today.add(key)
    except Exception:
        pass  # never propagate


def _record_api_stat(api_name: str) -> None:
    """
    Increment the daily API-call counter for api_name ("airlabs" or "aeroapi")
    in the SQLite api_calls table.  Called after a successful live API call.
    Never raises.
    """
    today = datetime.now(_PACIFIC).strftime("%Y-%m-%d")
    try:
        with _stats_lock:
            if _db_conn is not None:
                try:
                    _db_conn.execute(
                        """INSERT INTO api_calls(date, api_name, count) VALUES (?, ?, 1)
                           ON CONFLICT(date, api_name) DO UPDATE SET count = count + 1""",
                        (today, api_name),
                    )
                    _db_conn.commit()
                except Exception as exc:
                    _log(f"[overhead] DB api_stat failed for {api_name}: {exc}")
    except Exception:
        pass


def _record_free_api_check(callsign: str, free_route: str,
                            paid_route: str, matched: bool) -> None:
    """
    Persist one adsbdb vs. paid-API cross-check result to free_api_checks.
    Only called for commercial flights where both sides had data.
    Deduplicates: the same (callsign, free_route, paid_route) combination is
    only recorded once per _FREE_API_CHECK_DEDUP_SECS window so that repeated
    poll cycles while a flight is overhead don't inflate the counts.
    Runs under _stats_lock using _db_conn — same serialisation as sightings.
    Never raises.
    """
    if _db_conn is None:
        return
    # Dedup check — skip if same result was recorded recently for this callsign.
    # The dict update is inside _stats_lock so the read-check-write is atomic with
    # the DB insert, preventing duplicate rows from concurrent callers.
    _now_ts = time.time()
    try:
        _now    = datetime.now(_PACIFIC)
        _seen   = _now.strftime("%Y-%m-%d %H:%M:%S")
        _date   = _now.strftime("%Y-%m-%d")
        with _stats_lock:
            _last = _last_free_api_check.get(callsign)
            if _last and _last[0] == free_route and _last[1] == paid_route:
                if _now_ts - _last[2] < _FREE_API_CHECK_DEDUP_SECS:
                    return
            _bounded_put(_last_free_api_check, callsign, (free_route, paid_route, _now_ts))
            _db_conn.execute(
                """INSERT INTO free_api_checks
                       (seen_at, date, callsign, free_route, paid_route, matched)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (_seen, _date, callsign or "",
                 free_route or "", paid_route or "",
                 1 if matched else 0),
            )
            _db_conn.commit()
    except Exception:
        pass  # never propagate — stat writes must not affect route resolution


def _record_ga_free_api_check(registration: str, callsign: str, free_api: str,
                               free_route: str, fr24_route: str, matched: bool) -> None:
    """
    Persist one GA free-API vs. FR24 cross-check result to ga_free_api_checks.
    Only called for N-number aircraft when FR24 has route data to compare against.
    free_api is 'adsbdb' or 'opensky'; fr24_route is FR24's answer (the ground truth).
    Deduplicates: the same result is only recorded once per _FREE_API_CHECK_DEDUP_SECS
    window so repeated poll cycles don't inflate the counts.
    Runs under _stats_lock using _db_conn — same serialisation as sightings.
    Never raises.
    """
    if _db_conn is None:
        return
    # Dedup check — skip if same result was recorded recently for this reg + api.
    # The dict update is inside _stats_lock so the read-check-write is atomic.
    _now_ts   = time.time()
    _dedup_key = (registration, free_api)
    try:
        _now  = datetime.now(_PACIFIC)
        _seen = _now.strftime("%Y-%m-%d %H:%M:%S")
        _date = _now.strftime("%Y-%m-%d")
        with _stats_lock:
            _last = _last_ga_free_api_check.get(_dedup_key)
            if _last and _last[0] == free_route and _last[1] == fr24_route:
                if _now_ts - _last[2] < _FREE_API_CHECK_DEDUP_SECS:
                    return
            _bounded_put(_last_ga_free_api_check, _dedup_key, (free_route, fr24_route, _now_ts))
            _db_conn.execute(
                """INSERT INTO ga_free_api_checks
                       (seen_at, date, registration, callsign, free_api,
                        free_route, fr24_route, matched)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (_seen, _date, registration or "", callsign or "",
                 free_api or "", free_route or "", fr24_route or "",
                 1 if matched else 0),
            )
            _db_conn.commit()
    except Exception:
        pass  # never propagate


# ── Route cache TTL tiers ──────────────────────────────────────────────────────
# These constants are defined here (before _init_db()) so _migrate_legacy_caches()
# can reference them at startup.  Scheduled airline routes are very stable (7 days
# safe).  GA, helicopters, and charters get 1 hour — a GA plane can land, refuel,
# and depart to a completely different destination within that window.
# Negative-cache (miss) entries use ROUTE_MISS_TTL for real-time APIs (OpenSky,
# AirLabs, AeroAPI) so they retry quickly as new data arrives.  adsbdb is a
# static historical DB and uses ADSBDB_CACHE_TTL for both hits and misses.
_SCHEDULED_PREFIXES = frozenset([
    # US majors
    "AAL", "DAL", "UAL", "SWA", "ASA", "JBU", "NKS", "FFT", "SCX", "AAY",
    "HAL", "VRD",
    # US ULCCs / leisure / charter
    "MXY", "VXP", "JSX", "TWY",
    # Canadian regional / leisure
    "ROU",
    # US cargo (scheduled routes — 7-day TTL appropriate)
    "FDX", "UPS", "GTI", "ABX", "ASN", "PAC", "CKS", "WGN", "NCR", "SOU",
    "DHK", "AGX",
    # US charters / military contract
    "OCN", "OAE",
    # Canadian
    "ACA", "WJA", "POE", "FLE", "SWG",
    # Mexican
    "AMX", "VOI", "VIV",
    # European
    "BAW", "VIR", "AFR", "DLH", "KLM", "UAE", "QTR", "SIA", "EIN", "IBE",
    "CFG", "EDW", "THY", "ETD", "SWR", "AUA", "NAX", "EZY", "RYR", "TAP",
    "FIN", "BEL",
    # Asian / Pacific
    "KAL", "ANA", "JAL", "CPA", "EVA", "CCA", "CSN", "ANZ",
    # Latin American
    "CMP", "AVA",
    # Oceania
    "QFA",
    # Regional/commuter
    "SKW", "ENY", "RPA", "QXE", "ASH", "PDT", "JIA", "UCA", "CPZ", "MTN",
    "FLG",
])

# Cache TTLs — all overridable via config.py; defaults below.
try:
    from config import ADSBDB_CACHE_TTL
except Exception:
    ADSBDB_CACHE_TTL = 3600         # free/unlimited — keep short; fresh data costs nothing

try:
    from config import OPENSKY_CACHE_TTL
except Exception:
    OPENSKY_CACHE_TTL = 3600        # free/unlimited, hex-keyed — keep short

try:
    from config import ROUTE_TTL_SCHEDULED
except Exception:
    ROUTE_TTL_SCHEDULED = 604800    # 7 days — commercial + regional airlines (stable schedules)

try:
    from config import ROUTE_TTL_DEFAULT
except Exception:
    ROUTE_TTL_DEFAULT = 3600        # 1 hour — GA, helicopters, charters, unknown (can re-depart)

try:
    from config import ROUTE_MISS_TTL
except Exception:
    ROUTE_MISS_TTL = 300            # negative cache: retry after 5 min when an API has no data

try:
    from config import ROUTE_PAID_MISS_TTL
except Exception:
    ROUTE_PAID_MISS_TTL = 7200      # both paid APIs confirmed empty — suppress for 2 h

AIRCRAFT_CACHE_TTL = 86400  # aircraft type is static; 24 hr TTL
AIRCRAFT_MISS_TTL  = 300    # negative cache: don't hammer all 3 type APIs every poll cycle

# Open (or create) the SQLite DB — must run before _load_stats_seen_today()
# so the sightings table is available for the dedup rebuild.
# Must also run AFTER the TTL constants above, since _migrate_legacy_caches()
# references them during the one-time ft_cache.json import on first boot.
_init_db()
# Restore today's seen-callsigns from the sightings table (falls back to ft_stats.json).
_load_stats_seen_today()


def _route_ttl(callsign: str) -> int:
    """
    Return the positive-hit cache TTL for AirLabs/AeroAPI based on operator type.
    Keyed off the ICAO 3-letter prefix (first 3 chars of callsign).
    Scheduled airlines (commercial + regional) → 7 days.
    Everything else (GA, helicopters, charters) → 1 hour.
    """
    if not callsign or len(callsign) < 3:
        return ROUTE_TTL_DEFAULT
    if callsign[:3].upper() in _SCHEDULED_PREFIXES:
        return ROUTE_TTL_SCHEDULED
    return ROUTE_TTL_DEFAULT

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


def _has_local_endpoint(origin: str, dest: str) -> bool:
    """Return True if either airport code is in the configured local set.

    LOCAL MODE vs GLOBAL MODE — driven entirely by config.LOCAL_AIRPORTS:
      • LOCAL_AIRPORTS set (e.g. "LAS,VGT,HSH"): normal behavior — only routes
        touching a home airport are "local"; non-local routes are deferred and
        cross-checked so the most accurate local route wins.
      • LOCAL_AIRPORTS empty: every route counts as "local", which disables
        local-favouring entirely.  Each source then commits its first route
        inline (short-circuiting the rest in strict SOURCE_PRIORITY / hierarchy
        order) and routes cache normally.  For deployments with no "home" — e.g.
        worldwide traffic overhead — this just returns the best route any source
        has, polled in hierarchy order until one answers.
    """
    if not _LOCAL_AIRPORTS:
        return True
    return (
        bool(origin and origin.upper() in _LOCAL_AIRPORTS)
        or bool(dest   and dest.upper()   in _LOCAL_AIRPORTS)
    )


def _is_nonlocal(origin, dest):
    """True when a route is COMPLETE (both endpoints set) and neither is local."""
    return bool(origin and dest) and not _has_local_endpoint(origin, dest)


# Source trust priority for selecting among held non-local routes (lower = more
# trusted).  Paid real-time sources first, then FR24 (unofficial/scraped), then
# the free historical DBs last.  Declarative on purpose: reordering the resolution
# preference — or demoting/removing a source — is a one-line edit here.
SOURCE_PRIORITY = {
    "airlabs":  0,
    "airlabs2": 1,
    "aeroapi":  2,
    "fr24":     3,
    "adsbdb":   4,
    "opensky":  4,
    "adsbdb+opensky": 4,
}


# ── Backoff tuning constants (geometry constants live in utilities/geometry.py) ─
OPENSKY_LOOKBACK_SECS   = 6 * 3600  # OpenSky flights-by-aircraft lookback window (6 h)
BACKOFF_RATE_LIMIT_SECS = 3600      # 1 h backoff after a 429 rate-limit
BACKOFF_AUTH_SECS       = 86400     # 24 h backoff after an auth error (401/403)
# Quota exhausted (402, or an over-limit empty body): back off this long, then PROBE
# again — AirLabs allows usage past the nominal monthly limit and our local count can
# drift from theirs, so we re-test rather than hard-stopping on our own tally.
QUOTA_PROBE_BACKOFF_SECS = 24 * 3600


# ── Airport coordinate table (gives FR24 routes the same geometry check) ───────
# FR24's get_flights() returns IATA codes but NO airport coordinates, so its
# routes could never be plausibility-checked like every other source.  This table
# resolves IATA -> (lat, lon) and SELF-POPULATES from every other API (adsbdb,
# AirLabs, AeroAPI) that already returns airport coordinates; harvested entries
# persist in the cache DB (cache_type='airport', 1-year TTL) so they survive
# restarts.  FR24 §1/§5 consult it to reject a stale/wrong-leg route the same way
# the paid/free sources do.  Coords need only be accurate to ~1 km — well within
# the detour-ratio test's tolerance.
#
# _AIRPORT_SEED below is a GENERIC, EDITABLE cold-start convenience (the busiest
# US hubs, where stale hub-to-hub legs are most likely).  It contains NO personal
# location — the home airports are seeded separately, from config.py, just below.
# A forker outside the US can swap these for regional hubs or leave them as-is;
# the table self-populates from live data regardless, and any airport not in it
# simply falls through to benefit-of-the-doubt.
_AIRPORT_SEED = {
    "ATL": (33.6367, -84.4281),  "LAX": (33.9425, -118.4081), "ORD": (41.9786, -87.9048),
    "DFW": (32.8969, -97.0380),  "DEN": (39.8617, -104.6731), "JFK": (40.6413, -73.7781),
    "SFO": (37.6189, -122.3750), "SEA": (47.4502, -122.3088), "MCO": (28.4312, -81.3081),
    "MIA": (25.7959, -80.2870),  "PHX": (33.4342, -112.0116), "EWR": (40.6895, -74.1745),
    "IAH": (28.9844, -95.3414),  "BOS": (42.3656, -71.0096),  "MSP": (44.8848, -93.2223),
    "DTW": (42.2124, -83.3534),  "FLL": (26.0726, -80.1527),  "CLT": (35.2140, -80.9431),
    "LGA": (40.7769, -73.8740),  "PHL": (39.8744, -75.2424),  "BWI": (39.1754, -76.6683),
    "SLC": (40.7884, -111.9778), "SAN": (32.7338, -117.1933), "IAD": (38.9531, -77.4565),
    "DCA": (38.8512, -77.0402),  "MDW": (41.7868, -87.7522),  "TPA": (27.9755, -82.5332),
    "PDX": (45.5887, -122.5975), "HNL": (21.3187, -157.9224), "STL": (38.7487, -90.3700),
    "AUS": (30.1945, -97.6699),  "BNA": (36.1245, -86.6782),  "MCI": (39.2976, -94.7139),
    "RDU": (35.8776, -78.7875),  "SMF": (38.6954, -121.5908), "SJC": (37.3626, -121.9291),
    "SNA": (33.6757, -117.8678), "DAL": (32.8471, -96.8518),  "HOU": (29.6454, -95.2789),
    "OAK": (37.7126, -122.2197), "MSY": (29.9934, -90.2580),  "SAT": (29.5337, -98.4698),
    "RSW": (26.5362, -81.7552),  "CLE": (41.4117, -81.8498),  "PIT": (40.4915, -80.2329),
    "CVG": (39.0489, -84.6678),  "IND": (39.7173, -86.2944),  "CMH": (39.9980, -82.8919),
    "PBI": (26.6832, -80.0956),  "JAX": (30.4941, -81.6879),  "BDL": (41.9389, -72.6832),
    "ONT": (34.0560, -117.6012), "BUR": (34.2007, -118.3590), "ABQ": (35.0402, -106.6092),
    "TUS": (32.1161, -110.9410), "RNO": (39.4991, -119.7681), "BOI": (43.5644, -116.2228),
    "OKC": (35.3931, -97.6007),  "TUL": (36.1984, -95.8881),  "OMA": (41.3032, -95.8941),
    "MEM": (35.0424, -89.9767),  "ELP": (31.8072, -106.3781), "ANC": (61.1743, -149.9982),
    "GEG": (47.6199, -117.5338), "ORF": (36.8946, -76.2012),  "RIC": (37.5052, -77.3197),
    "BHM": (33.5629, -86.7535),  "YYZ": (43.6772, -79.6306),  "YVR": (49.1939, -123.1844),
    "CUN": (21.0365, -86.8771),  "GDL": (20.5218, -103.3112), "MEX": (19.4363, -99.0721),
}
_airport_coords_mem = dict(_AIRPORT_SEED)  # IATA -> (lat, lon); grows at runtime

# Config-driven home-airport seed: give each LOCAL_AIRPORTS code from config.py an
# approximate coordinate (the receiver's home location, LOCATION_HOME) so a route
# through a home field can be geometry-checked from the very first poll.  These are
# refined to exact coordinates automatically the first time any API returns the
# airport (_remember_airport overwrites on a coordinate change).  setdefault keeps
# a precise coordinate if a home airport already appears in the generic seed above.
# Nothing here hardcodes a location — it all derives from the (gitignored) config.
try:
    _home_lat = float(LOCATION_DEFAULT[0])
    _home_lon = float(LOCATION_DEFAULT[1])
    for _home_apt in _LOCAL_AIRPORTS:
        _airport_coords_mem.setdefault(_home_apt, (_home_lat, _home_lon))
except Exception:
    pass


def _remember_airport(iata, lat, lon):
    """Record an IATA->coords mapping harvested from a source that returned coords."""
    if not iata or lat is None or lon is None:
        return
    iata = iata.strip().upper()
    if len(iata) != 3 or not iata.isalpha():
        return
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0) or (lat == 0.0 and lon == 0.0):
        return
    if _airport_coords_mem.get(iata) == (lat, lon):
        return  # already known and unchanged — skip the DB write
    _airport_coords_mem[iata] = (lat, lon)
    # Persist with a long TTL (airport coords are static) so it survives restarts.
    _cache_db_set_route(f"apt:{iata}", 'airport', iata, '', lat, lon, None, None,
                        int(time.time()) + 365 * 86400, source="harvest")


def _airport_coords(iata):
    """Return (lat, lon) for an IATA code, or None.  Checks memory, then the DB."""
    if not iata:
        return None
    iata = iata.strip().upper()
    coords = _airport_coords_mem.get(iata)
    if coords:
        return coords
    row = _cache_db_get_route(f"apt:{iata}", 'airport')
    if row and row[2] is not None and row[3] is not None:
        coords = (row[2], row[3])
        _airport_coords_mem[iata] = coords
        return coords
    return None


def _fr24_route_plausible(plane_lat, plane_lon, o_iata, d_iata):
    """Geometry check for FR24 routes (which carry no coordinates).

    Resolves the origin/dest IATA codes to coordinates via the airport table and
    applies the same detour-ratio test (_route_plausible) the other sources use.
    Returns True when either airport's coords are unknown — benefit of the doubt,
    matching _route_plausible's own missing-coordinate behavior (no regression).
    """
    o = _airport_coords(o_iata)
    d = _airport_coords(d_iata)
    if not o or not d:
        return True  # unknown airport(s) — can't validate, assume plausible
    return _route_plausible(plane_lat, plane_lon, o[0], o[1], d[0], d[1])


# ── Route candidate model + unified selection ─────────────────────────────────────
# A source FETCHER produces one _Cand (or None) describing what it found — origin/dest,
# optional airport coords (for geometry), its source name, and whether it was a live call.
# It does NOT commit anything.  _select() is the SINGLE place a route is chosen from the
# gathered candidates — sole authority in get_route() as of the Phase-3 flip, validated by
# a 4-day route shadow soak (1,437/0) + a second ~660/0 live soak, then made permanent: the
# [flip-check] shadow backstop was retired once it had logged zero disagreements.  The inline
# per-source commits remain ONLY to short-circuit paid API calls and to complete override-
# partials (a path _select() intentionally does not handle) — they no longer pick the route.
_Cand = namedtuple("_Cand", "origin dest olat olon dlat dlon source is_live")


def _route_tier(origin, dest):
    """Route-quality tier, lower = better:
        0 local-complete, 1 local-partial, 2 non-local-complete, 3 non-local-partial.
    A LOCAL endpoint outranks completeness (prefer-local); within a locality a complete
    route beats a partial one.  In GLOBAL mode (_has_local_endpoint always True) every
    route is tier 0/1, which disables local-favouring exactly as the live code does."""
    return (0 if _has_local_endpoint(origin, dest) else 2) + (0 if (origin and dest) else 1)


def _cand_plausible(cand, plane_lat, plane_lon):
    """Geometry check for a candidate: use its OWN coords when present (most precise),
    else resolve its IATA codes through the harvested airport-coords table.  Returns True
    when coords are unknown — benefit of the doubt, matching each source's own behavior."""
    if cand.olat is not None and cand.dlat is not None:
        return _route_plausible(plane_lat, plane_lon,
                                cand.olat, cand.olon, cand.dlat, cand.dlon)
    return _fr24_route_plausible(plane_lat, plane_lon, cand.origin, cand.dest)


def _select(cands, plane_lat, plane_lon):
    """Pick the single best route candidate from `cands`, or None.

      1. Drop empties and geometry-implausible candidates (the anti-stale-route guard
         that keeps a wrong/old leg — the "random PHX" routes — from ever winning).
      2. Rank the survivors by (route tier, SOURCE_PRIORITY): a local endpoint beats
         completeness, and within a tier the more-trusted source wins.

    This is the ONE place route selection happens.  Sources only produce candidates; they
    never commit-vs-defer inline — which is the entire point of the structural refactor."""
    usable = [c for c in cands
              if (c.origin or c.dest) and _cand_plausible(c, plane_lat, plane_lon)]
    if not usable:
        return None
    usable.sort(key=lambda c: (_route_tier(c.origin, c.dest),
                               SOURCE_PRIORITY.get(c.source, 99)))
    best = usable[0]
    # Cross-source combine: when the winner is a LOCAL route still missing one endpoint,
    # fill that endpoint from the next-best source that supplies it AND is not a deferred
    # non-local-complete route — mirroring the live "trusted local origin + a destination
    # from another source" merge (e.g. OpenSky LAS->? + AirLabs ?->JFK = LAS->JFK), while
    # NOT grafting a stale non-local leg's endpoint onto it (e.g. a deferred SYR->CHS).
    if _has_local_endpoint(best.origin, best.dest) and not (best.origin and best.dest):
        for c in usable[1:]:
            if _is_nonlocal(c.origin, c.dest):
                continue  # a non-local-complete route is deferred — it never fills a blank
            if not best.dest and c.dest:
                best = best._replace(dest=c.dest, source=f"{best.source}+{c.source}")
                break
            if not best.origin and c.origin:
                best = best._replace(origin=c.origin, source=f"{c.source}+{best.source}")
                break
    return best


# ── Locks and in-memory session state ─────────────────────────────────────────
_cache_lock  = Lock()   # guards _cache_conn + _opensky_token
_usage_lock  = Lock()   # guards _airlabs_increment / _aeroapi_increment read-then-write
# Cache key scheme used in the 'route' cache_type:
#   callsign           — adsbdb result  (e.g. "SWA123")
#   hex_code           — OpenSky result (e.g. "a1b2c3")
#   "airlabs:callsign" — AirLabs result (prefixed to avoid collisions with adsbdb)
_opensky_token = {"value": None, "expires_at": 0, "fetching": False}

# ── API backoff state ──────────────────────────────────────────────────────────
# Driven by actual HTTP responses, not local counters.
# In-memory only — resets on service restart, which is intentional
# (a fresh restart should retry APIs rather than carry over a stale block).
_api_backoff: dict[str, float] = {}  # api_name -> epoch time to stop backing off
_api_credit_exhausted: dict[str, str] = {}  # api_name -> billing period string when a 402 was received

# Display names for API keys in log messages (internal key → human-readable tag).
_API_LOG_NAME: dict[str, str] = {"airlabs": "airlabs-1", "airlabs2": "airlabs-2"}

def _display_src(src: str) -> str:
    """Remap internal source strings to human-readable display names for log output.
    Internal keys (airlabs, airlabs2) are kept as-is in the DB and cache.  The route
    display collapses BOTH AirLabs keys to a single 'airlabs' label — which key
    answered doesn't matter for the route, and the per-source [airlabs-1]/[airlabs-2]
    step logs already record that detail.  (The previous airlabs-1/airlabs-2 mapping
    double-substituted: 'airlabs2' → 'airlabs-2' → 'airlabs-1-2'.)
    Works on compound sources like 'resolved:airlabs2:cached' or 'adsbdb+airlabs2'.
    """
    return src.replace("airlabs2", "airlabs")

# Cross-check dedup: suppress re-recording the same result while a flight is
# still overhead.  Resets on restart (intentional — fresh start = fresh stats).
_FREE_API_CHECK_DEDUP_SECS = 1800  # 30 minutes
_DEDUP_MAP_CAP = 2048              # hard cap on each per-callsign dedup map (24/7 leak guard)


def _bounded_put(d: dict, key, value) -> None:
    """Insert into a module-level per-callsign dedup map, evicting the oldest entry once it
    exceeds _DEDUP_MAP_CAP so these maps can't grow without bound on a 24/7 process.  FIFO
    is fine here: evicting a long-stale callsign at worst costs one extra log/stat line if
    it is ever seen again."""
    d[key] = value
    if len(d) > _DEDUP_MAP_CAP:
        try:
            del d[next(iter(d))]
        except (StopIteration, KeyError):
            pass


# callsign -> (free_route, paid_route, last_recorded_ts)
_last_free_api_check:    dict[str, tuple] = {}
# (registration, free_api) -> (free_route, fr24_route, last_recorded_ts)
_last_ga_free_api_check: dict[tuple, tuple] = {}
# (api_name, callsign) -> last_logged_ts  — suppresses repeat "in backoff" log spam
_last_backoff_log: dict[tuple, float] = {}
# callsign -> last-logged route signature; suppresses repeating the identical [route:..]
# display line every poll for a lingering flight (the [overhead] alt lines still show it).
_last_route_log: dict[str, tuple] = {}
# callsign -> last-logged override-match signature; same idea for the [override] match
# lines, so a lingering override flight (e.g. JANET77 orbiting) logs the match once and
# then only the [overhead] alt= tracking line repeats until the rule (or its result) changes.
_last_override_log: dict[str, tuple] = {}

def _in_backoff(api_name: str) -> bool:
    """True if we should skip this API because it recently told us to back off."""
    until = _api_backoff.get(api_name, 0.0)
    return time.time() < until

def _set_backoff(api_name: str, secs: int = 3600) -> None:
    """Record a backoff period after receiving a rate-limit or auth error."""
    _api_backoff[api_name] = time.time() + secs
    _display = _API_LOG_NAME.get(api_name, api_name)
    _until   = datetime.fromtimestamp(_api_backoff[api_name], _PACIFIC).strftime("%Y-%m-%d %H:%M")
    if secs >= 86400:
        _dur = f"{secs / 86400:.1f} d"
    elif secs >= 3600:
        _dur = f"{secs / 3600:.1f} h"
    else:
        _dur = f"{secs // 60} min"
    _log(f"[{_display}] backing off until {_until} ({_dur})")

def _check_period_reset(api_name: str, reset_day: int) -> None:
    """If this API is in backoff due to a 402 and a new billing period has since
    started, clear the backoff so the API resumes immediately rather than waiting
    up to 24 h for the old backoff timer to expire."""
    # Read the stamp into a local first: this runs on multiple lookup threads, and a
    # check-then-subscript (`in` … then `[api_name]`) could KeyError if another thread
    # pops the key in between.  `.get()` + `.pop(…, None)` make it race-safe.
    exhausted_period = _api_credit_exhausted.get(api_name)
    if exhausted_period is None or not _in_backoff(api_name):
        return
    current_period = _billing_period_start(reset_day)
    if exhausted_period != current_period:
        _api_backoff.pop(api_name, None)
        _api_credit_exhausted.pop(api_name, None)
        _log(f"[{api_name}] new billing period — credit backoff cleared, resuming")



# ── Override rules ─────────────────────────────────────────────────────────────
# Stored in SQLite (overrides + overrides_meta tables).  A version counter in
# overrides_meta is incremented on every save; _load_overrides() checks it on
# each call and reloads from DB only when it has changed.
# Lock ordering (always outermost → innermost): _overrides_lock → _cache_lock.
_overrides_lock:   Lock = Lock()
_overrides_cache:  list = []
_overrides_version: int = -1   # -1 = not yet loaded from DB


def _load_overrides() -> list:
    """Return the current override rules, reloading from DB when the version counter changes.
    Thread-safe: callers may be any of the ThreadPoolExecutor worker threads.
    Returns a snapshot copy so callers can iterate without holding either lock.
    """
    global _overrides_cache, _overrides_version
    with _overrides_lock:
        if _cache_conn is not None:
            try:
                with _cache_lock:
                    row = _cache_conn.execute(
                        "SELECT value FROM overrides_meta WHERE key='version'"
                    ).fetchone()
                    db_version = int(row[0]) if row else 0
                    if db_version != _overrides_version:
                        rows = _cache_conn.execute(
                            "SELECT pattern, origin, destination, display, plane, note "
                            "FROM overrides ORDER BY position, id"
                        ).fetchall()
                        _overrides_cache = [
                            {
                                "pattern":     r[0],
                                "origin":      r[1],
                                "destination": r[2],
                                "display":     r[3],
                                "plane":       r[4],
                                "note":        r[5],
                            }
                            for r in rows
                        ]
                        _overrides_version = db_version
            except Exception as e:
                _log(f"[override] WARNING: failed to load overrides from DB: {e}")
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
    """True when src represents a live (non-cached) API call."""
    return ":cached" not in src and src not in ("none", "miss", "override")


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
                token_value = _opensky_token["value"]
            return token_value
        elif r.status_code == 429:
            _set_backoff("opensky", secs=BACKOFF_RATE_LIMIT_SECS)
            _log("[opensky] token: 429 rate-limit — backing off 1 h")
        elif r.status_code in (401, 403):
            _set_backoff("opensky", secs=BACKOFF_AUTH_SECS)
            _log(f"[opensky] token: auth error {r.status_code} — backing off 24 h")
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
    def __init__(self, lat, lon, altitude, vertical_speed, callsign, hex_code="", registration=""):
        self.latitude = lat
        self.longitude = lon
        self.altitude = altitude
        self.vertical_speed = vertical_speed
        self.callsign = callsign
        # Normalize hex to lowercase: it's used as a cache key (OpenSky route, reg)
        # and the APIs are queried lowercased, so an uppercase source would otherwise
        # fragment the cache into two rows for the same airframe.
        self.hex_code = hex_code.strip().lower() if hex_code else ""
        self.registration = registration.strip().upper() if registration else ""
        # VRS-only fields — populated by from_vrs(); empty strings for all other sources.
        self.vrs_origin = ""
        self.vrs_dest   = ""
        self.vrs_type   = ""

    @classmethod
    def from_fr24(cls, hex_code, entry):
        try:
            lat = entry[FR24_LAT]
            lon = entry[FR24_LON]
            if lat is None or lon is None:
                return None
            alt = entry[FR24_ALT]
            reg = entry[FR24_REG] if len(entry) > FR24_REG else ""
            return cls(
                lat=lat,
                lon=lon,
                altitude=alt if isinstance(alt, (int, float)) else 0,
                vertical_speed=entry[FR24_VERT] if isinstance(entry[FR24_VERT], (int, float)) else 0,
                callsign=(entry[FR24_CALLSIGN] or "").strip(),
                hex_code=hex_code,
                registration=reg or "",
            )
        except (IndexError, TypeError):
            return None

    @classmethod
    def from_dump1090(cls, ac):
        lat = ac.get("lat")
        lon = ac.get("lon")
        if lat is None or lon is None:
            return None
        alt = ac.get("alt_baro", 0)
        return cls(
            lat=lat,
            lon=lon,
            altitude=alt if isinstance(alt, (int, float)) else 0,
            vertical_speed=ac.get("baro_rate", ac.get("geom_rate", 0)) or 0,
            callsign=(ac.get("flight") or "").strip(),
            hex_code=ac.get("hex", ""),
            registration=ac.get("registration", "") or "",
        )

    @classmethod
    def from_vrs(cls, ac):
        """Build a Flight from a Virtual Radar Server AircraftList entry.

        VRS AircraftList.json field reference:
          Icao  — ICAO24 hex code
          Call  — callsign
          Alt   — barometric altitude (feet)
          Lat / Long — position
          Vsi   — vertical speed (feet/min)
          Reg   — registration / tail number
          From  — origin airport string, e.g. "KLAS Las Vegas Harry Reid Intl"
          To    — destination airport string
          Mdl   — aircraft model description (e.g. "Boeing 737-800")
          Type  — ICAO type code (e.g. "B738")
        """
        lat = ac.get("Lat")
        lon = ac.get("Long")
        if lat is None or lon is None:
            return None
        alt = ac.get("Alt", 0)
        try:
            _lat_f = float(lat)
            _lon_f = float(lon)
        except (ValueError, TypeError):
            return None
        obj = cls(
            lat=_lat_f,
            lon=_lon_f,
            altitude=alt if isinstance(alt, (int, float)) else 0,
            vertical_speed=ac.get("Vsi", 0) or 0,
            callsign=(ac.get("Call") or "").strip(),
            hex_code=(ac.get("Icao") or "").strip().lower(),
            registration=(ac.get("Reg") or "").strip().upper(),
        )
        # VRS-specific route and type hints — used in get_route() when available.
        obj.vrs_origin = _vrs_airport_to_iata(ac.get("From") or "")
        obj.vrs_dest   = _vrs_airport_to_iata(ac.get("To")   or "")
        # Prefer the human-readable model description; fall back to ICAO type code.
        obj.vrs_type   = (ac.get("Mdl") or ac.get("Type") or "").strip()
        return obj


# ── Data fetching ──────────────────────────────────────────────────────────────

def fetch_flights():
    """Return Flight objects from the configured ADS-B receiver source.

    RECEIVER_TYPE = "dump1090" (default): tries fr24feed first, then dump1090.
    RECEIVER_TYPE = "vrs": polls Virtual Radar Server's AircraftList JSON API.
    """
    # ── Virtual Radar Server mode ──────────────────────────────────────────────
    if RECEIVER_TYPE == "vrs":
        try:
            r = requests.get(VRS_URL, timeout=3)
            if r.status_code == 200:
                flights = []
                for ac in r.json().get("acList", []):
                    f = Flight.from_vrs(ac)
                    if f:
                        flights.append(f)
                return flights
        except Exception:
            pass
        _log(f"[overhead] VRS receiver unreachable — no data from {RECEIVER_HOST}")
        return []

    # ── dump1090 / fr24feed mode (default) ────────────────────────────────────
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
    today = datetime.now(_PACIFIC)
    if today.day >= reset_day:
        # Clamp to actual days in this month (e.g. reset_day=31 in February)
        this_month_days = calendar.monthrange(today.year, today.month)[1]
        safe_day = min(reset_day, this_month_days)
        return today.replace(day=safe_day).strftime("%Y-%m-%d")
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


def _airlabs_increment():
    with _usage_lock:
        data = _read_usage(AIRLABS_USAGE_FILE, AIRLABS_RESET_DAY)
        data["value"] = data.get("value", 0) + 1
        _write_usage(AIRLABS_USAGE_FILE, data)
        remaining = AIRLABS_MONTHLY_LIMIT - data["value"]
        count     = int(data["value"])
    if remaining <= 50:
        _log(f"[airlabs-1] WARNING: {int(remaining)} calls remaining this period")
    _record_api_stat("airlabs")
    return count  # returned for inline logging


def _airlabs2_increment():
    with _usage_lock:
        data = _read_usage(AIRLABS2_USAGE_FILE, AIRLABS2_RESET_DAY)
        data["value"] = data.get("value", 0) + 1
        _write_usage(AIRLABS2_USAGE_FILE, data)
        remaining = AIRLABS2_MONTHLY_LIMIT - data["value"]
        count     = int(data["value"])
    if remaining <= 50:
        _log(f"[airlabs-2] WARNING: {int(remaining)} calls remaining this period")
    _record_api_stat("airlabs2")
    return count  # returned for inline logging


def _aeroapi_increment():
    with _usage_lock:
        data = _read_usage(AEROAPI_USAGE_FILE, AEROAPI_RESET_DAY)
        data["value"] = round(data.get("value", 0.0) + AEROAPI_COST_PER_CALL, 4)
        _write_usage(AEROAPI_USAGE_FILE, data)
    _log(f"[aeroapi] period spend so far: ~${data['value']:.3f}")
    _record_api_stat("aeroapi")


def _restore_persisted_backoff(backoff_name, usage_file, reset_day):
    """Reload a persisted quota-probe backoff into the in-memory backoff dict.
    The in-memory _api_backoff is lost on restart; persisting the probe window to the
    usage file keeps the ~24 h daily-probe cadence ANCHORED, so a restart neither
    resets it nor pushes it forward.  A new billing period clears it automatically
    (_read_usage drops the field once the period rolls over)."""
    if _in_backoff(backoff_name):
        return  # an active in-memory backoff already covers this key
    with _usage_lock:
        until = _read_usage(usage_file, reset_day).get("backoff_until", 0)
    if until and float(until) > time.time():
        _api_backoff[backoff_name] = float(until)
        # Also restore the billing-period stamp.  _api_credit_exhausted is in-memory only
        # and lost on restart; without it _check_period_reset bails on its first guard and
        # won't early-clear this backoff at the next period rollover — the key would then
        # needlessly wait out the full persisted window instead of resuming at reset.
        _api_credit_exhausted[backoff_name] = _billing_period_start(reset_day)


def _set_quota_backoff(backoff_name, usage_file, reset_day, log_tag, reason):
    """Back off a quota-exhausted AirLabs key for ONE probe interval (~24 h) and
    persist the expiry so it survives restarts.  After it lapses the next poll makes a
    real call to re-test whether AirLabs is serving again — it permits usage past the
    nominal limit, so we probe rather than stop on our own (drift-prone) count."""
    _set_backoff(backoff_name, secs=QUOTA_PROBE_BACKOFF_SECS)
    _api_credit_exhausted[backoff_name] = _billing_period_start(reset_day)
    until_ts = time.time() + QUOTA_PROBE_BACKOFF_SECS  # ~= what _set_backoff just stored
    with _usage_lock:
        u = _read_usage(usage_file, reset_day)
        u["backoff_until"] = until_ts
        _write_usage(usage_file, u)
    _log(f"[{log_tag}] {reason} — probing again in {QUOTA_PROBE_BACKOFF_SECS // 3600} h")


def _query_airlabs(callsign, plane_lat, plane_lon, *, api_key, cache_key, backoff_name,
                   usage_file, reset_day, monthly_limit, increment_fn, log_tag):
    """Fetch a route from one AirLabs key (key 1 or key 2).

    Handles the cache read, monthly-period reset, persisted quota-probe backoff,
    the HTTP call + status ladder (429/402/401/403/200/other), JSON parse, airport
    harvest, cache write (local vs non-local TTL), and the in-backoff log.  Returns
    (origin, dest, olat, olon, dlat, dlon, src, count); count > 0 means a live,
    billable call was made.  Commit-vs-defer is the caller's responsibility.
    """
    origin = dest = ""
    olat = olon = dlat = dlon = None
    src = backoff_name
    count = 0
    _cached = _cache_db_get_route(cache_key, 'route')
    _check_period_reset(backoff_name, reset_day)
    _restore_persisted_backoff(backoff_name, usage_file, reset_day)
    if _cached:
        origin, dest = _cached[0], _cached[1]
        olat, olon = _cached[2], _cached[3]
        dlat, dlon = _cached[4], _cached[5]
        src = f"{backoff_name}:cached"
    elif not _in_backoff(backoff_name):
        # No pre-call hard stop on our LOCAL count.  AirLabs permits usage past the
        # nominal monthly limit and our tally can drift from theirs, so we PROBE on
        # every non-backed-off poll and let AirLabs be the authority.  A 402 or an
        # over-limit empty response triggers a ~24 h backoff (persisted via
        # _set_quota_backoff so it doesn't drift on restart); the next poll after it
        # lapses probes again to see whether credit has freed up.
        try:
            r = requests.get(
                AIRLABS_URL,
                params={"flight_icao": callsign, "api_key": api_key},
                timeout=5,
            )
            if r.status_code == 429:
                _set_backoff(backoff_name, secs=BACKOFF_RATE_LIMIT_SECS)
            elif r.status_code == 402:
                _set_quota_backoff(backoff_name, usage_file, reset_day, log_tag,
                                   f"{callsign}: 402 — AirLabs rejected the call (over quota)")
            elif r.status_code in (401, 403):
                _set_backoff(backoff_name, secs=BACKOFF_AUTH_SECS)
                _log(f"[{log_tag}] auth error ({r.status_code}) — check the AirLabs API key")
            elif r.status_code == 200:
                count = increment_fn()
                resp = r.json().get("response") or {}
                origin = resp.get("dep_iata", "") or ""
                dest   = resp.get("arr_iata", "") or ""
                olat = resp.get("dep_lat")
                olon = resp.get("dep_lng")
                dlat = resp.get("arr_lat")
                dlon = resp.get("arr_lng")
                _remember_airport(origin, olat, olon)
                _remember_airport(dest,   dlat, dlon)
                _over_quota_empty = False
                if not (origin or dest):
                    _log(f"[{log_tag}] {callsign}: no data [call #{count}]")
                    # AirLabs soft-limits with a 200-empty (not a 402).  Once we've
                    # passed the nominal limit AND get an empty body, treat it as quota
                    # exhaustion and back off for ONE probe interval — the call after it
                    # re-tests whether AirLabs is serving again.
                    if count >= monthly_limit:
                        _set_quota_backoff(backoff_name, usage_file, reset_day, log_tag,
                                           f"{callsign}: over quota ({count}/{monthly_limit}) and no data")
                        # Over quota → AirLabs never actually consulted the backend, so
                        # this empty is meaningless.  Do NOT cache it: a cached empty
                        # would later trip the §3b secondary-key gate (_al1_cache_was_empty)
                        # into assuming the shared backend has no data, skipping the
                        # still-quota'd second key and leaking the lookup to the costlier
                        # AeroAPI.  (A genuine WITHIN-quota empty is still cached below so
                        # the shared-backend key 2 isn't queried for data that truly
                        # doesn't exist.)
                        _over_quota_empty = True
                # Non-local complete routes get ROUTE_MISS_TTL — almost certainly
                # wrong for a local-airport tracker and must not persist.  Local
                # or partial results get ROUTE_TTL_DEFAULT.
                if not _over_quota_empty:
                    _ttl = (
                        ROUTE_MISS_TTL if _is_nonlocal(origin, dest)
                        else ROUTE_TTL_DEFAULT if (origin or dest)
                        else ROUTE_MISS_TTL
                    )
                    _cache_db_set_route(cache_key, 'route', origin, dest,
                                        olat, olon, dlat, dlon,
                                        int(time.time()) + _ttl, source=backoff_name)
            else:
                # Unexpected status (e.g. 404, 500) — count the call (AirLabs may
                # bill any request) and negatively cache to stop per-poll retries.
                count = increment_fn()
                _log(f"[{log_tag}] {callsign}: unexpected status {r.status_code} [call #{count}] — negative caching")
                _cache_db_set_route(cache_key, 'route', "", "", None, None, None, None,
                                    int(time.time()) + ROUTE_MISS_TTL, source=backoff_name)
        except Exception as e:
            _log(f"[{log_tag}] {callsign}: request error — {e}")
    else:
        _bl_key = (backoff_name, callsign)
        if time.time() - _last_backoff_log.get(_bl_key, 0) > _FREE_API_CHECK_DEDUP_SECS:
            _until = datetime.fromtimestamp(_api_backoff.get(backoff_name, 0), _PACIFIC).strftime("%Y-%m-%d %H:%M")
            _log(f"[{log_tag}] {callsign}: in backoff until {_until} — skipping")
            _bounded_put(_last_backoff_log, _bl_key, time.time())
    return origin, dest, olat, olon, dlat, dlon, src, count


def get_route(hex_code, callsign, vertical_speed, plane_lat=None, plane_lon=None,
              vrs_origin="", vrs_dest="", registration="", _trace=None):
    """
    Route lookup priority (first validated route wins; most resolve early):
      0.    Override rules (ft_overrides.json) — full override returns immediately;
            partial seeds the known endpoint(s) and skips paid APIs; display-only
            (no endpoints) passes through to all APIs.
      0.5   Resolved-route cache (scheduled airlines, 7-day TTL) — local-origin hits
            trusted; non-local hits geometry-checked or busted.
      0.75  VRS live hint — receiver feed supplies a route with >=1 local endpoint.
      1.    FlightRadar24 GA — N-number / non-commercial, by registration.  Geometry-
            checked against the harvested airport-coordinate table.  No local-origin
            restriction (a tail number identifies the aircraft uniquely).
      2.    adsbdb — static historical DB by callsign; trusted only when the ORIGIN
            is a local airport AND geometry passes.  Fills blanks only.
      2a.   OpenSky — free, real-time by ICAO24 hex; local-origin trust; fills blanks.
      2b.   Free-API consensus — adsbdb + OpenSky agree on the same route AND geometry
            passes → trusted without a paid call (see the limitation note at §2b).
      2c.   GA cross-check — records FR24-vs-free-API agreement (stats only).
      3.    AirLabs-1 — real-time by callsign (1,000/mo free); any plausible route.
      3b.   AirLabs-2 — secondary key; runs only when AirLabs-1 made no live call (or
            is over quota) and hadn't already confirmed an empty/implausible result.
      4.    FlightAware AeroAPI — paid last resort; any plausible route.  Also runs
            when AirLabs HELD a non-local route, to try for a local-airport route.
      5.    FlightRadar24 commercial — free last resort by registration.  Geometry-
            checked against the airport-coordinate table.  Fills blanks only.
      If no source supplies a validated route, origin/dest → "?".

    Non-local deferral: a complete route with NEITHER endpoint local is HELD (not
    committed) by AirLabs/AeroAPI/FR24 and cross-checked down the chain.  If no
    source yields a local route, the best-priority non-local result (AeroAPI >
    AirLabs-2 > AirLabs-1 > FR24) is shown at a short TTL and never written to the
    resolved cache — only local routes are persisted.

    Trust rule: free APIs (adsbdb, OpenSky) require a LOCAL ORIGIN.  Paid APIs and
    FR24 trust any plausible route.  ~90%+ of in-zone overhead traffic departs a
    configured local airport.

    Cache format: (origin, dest, orig_lat, orig_lon, dest_lat, dest_lon, source).
    Negative cache entries have empty origin/dest with None coords — used to avoid
    re-querying APIs within ROUTE_MISS_TTL when they had no data.

    plane_lat/plane_lon: aircraft's current position, used for plausibility checks.
    vertical_speed: kept for API compatibility; no longer used in trust logic.
    """
    origin, destination, source = "", "", ""
    now = int(time.time())
    _apis_disabled = os.path.exists(APIS_DISABLED_FLAG)  # evaluate once — two callers below
    # Best origin/dest coords seen during this lookup — stored in the resolved cache
    # so future hits can run a geometry plausibility check even without a live API call.
    _coord_olat = _coord_olon = _coord_dlat = _coord_dlon = None
    _coord_origin_iata = ""  # tracks which airport _coord_olat belongs to

    # Override display/type fields — populated if any override rule matches;
    # returned at the end so partial overrides (missing endpoints) still carry
    # display name and aircraft type through the free-API fill-in path.
    _ov_plane   = ""
    _ov_display = ""
    ov_origin   = ""
    ov_dest     = ""
    _override_partial = False  # True → skip paid APIs but still try free APIs

    # ── 0. Override rules — bypass ALL API lookups for known callsigns ─────────
    # Rules are defined in ft_overrides.json and managed via the web UI.
    # Patterns are case-insensitive; * is a wildcard (e.g. JANET* matches any
    # Janet flight).
    # • Full override (both origin + destination set): return immediately.
    # • Partial override (one or both endpoints missing): seed what we have,
    #   continue through the free APIs (adsbdb, OpenSky) to fill blanks, but
    #   skip paid APIs (AirLabs, AeroAPI) — overrides are typically GA/special
    #   flights that paid APIs won't have on record anyway.
    _ov = _match_override(callsign)
    if _ov:
        ov_origin   = (_ov.get("origin")      or "").strip().upper()
        ov_dest     = (_ov.get("destination") or "").strip().upper()
        _ov_plane   = (_ov.get("plane")       or "").strip()
        _ov_display = (_ov.get("display")     or "").strip()
        # Log the match only on first sighting or when the rule/result changes — not every
        # poll — so a lingering override flight doesn't repeat identical [override] lines
        # every ~15 s.  No-cache/test lookups always log (full diagnostic).  The [overhead]
        # alt= tracking line still prints every poll either way.
        _ov_sig = (_ov.get("pattern"), ov_origin, ov_dest, _ov_display, _ov_plane, _ov.get("note"))
        if getattr(_cache_bypass, "on", False):
            _ov_first = True
        else:
            _ov_first = _last_override_log.get(callsign) != _ov_sig
            if _ov_first:
                _bounded_put(_last_override_log, callsign, _ov_sig)
        if _ov_first:
            _log(
                f"[override] {callsign} matched '{_ov['pattern']}'"
                f" → {ov_origin or '?'}->{ov_dest or '?'}"
                + (f"  display='{_ov_display}'" if _ov_display else "")
                + (f"  type='{_ov_plane}'" if _ov_plane else "")
                + (f"  ({_ov['note']})" if _ov.get("note") else "")
            )
        if ov_origin and ov_dest:
            # Both endpoints known — no API calls needed.
            return ov_origin, ov_dest, "override", _ov_plane, _ov_display
        # Partial — seed available endpoints and fall through to free APIs.
        origin      = ov_origin
        destination = ov_dest
        source      = "override" if (ov_origin or ov_dest) else ""
        _override_partial = bool(ov_origin or ov_dest)
        if _ov_first:
            _log(f"[override] {callsign}: partial override — polling free APIs to fill missing endpoint(s)")

    # ── 0.5. Resolved-route cache (scheduled airlines only) ───────────────────
    # When we successfully resolve both endpoints for a scheduled airline
    # callsign (from any combination of sources), the final result is cached
    # here at the scheduled-airline TTL (7 days).  A hit normally skips the
    # entire API chain.
    #
    # Geometry guard: airline flight numbers are reused across different
    # city-pairs on different days (e.g. AAL2038 = SNA→ORD one day,
    # LAS→CLT the next).  To catch a stale cached route:
    #   • Local-origin departures are trusted unconditionally — we know our
    #     own airport's schedule.
    #   • Non-local origins with stored coordinates are validated with the
    #     same detour-ratio check used by AirLabs/AeroAPI.  A failing check
    #     busts the entry and falls through to a fresh lookup.
    #   • Non-local entries without coordinates (written before this fix) are
    #     also busted so they get re-resolved and stored with coordinates.
    if callsign and _route_ttl(callsign) == ROUTE_TTL_SCHEDULED and not (_override_partial and (ov_origin or ov_dest)):
        _resolved = _cache_db_get_route(callsign, 'resolved')
        if _resolved and _resolved[0] and _resolved[1]:
            _rsrc           = _resolved[6].removesuffix(":cached")
            _resolved_label = f"resolved:{_rsrc}:cached" if _rsrc else "resolved:cached"
            _res_origin     = _resolved[0].upper()
            # Local departures: trust immediately — no geometry needed.
            if _res_origin in _LOCAL_AIRPORTS:
                return _resolved[0], _resolved[1], _resolved_label, _ov_plane, _ov_display
            # Non-local: validate with stored coordinates, or bust if none.
            if _resolved[2] is not None:
                if _route_plausible(plane_lat, plane_lon,
                                    _resolved[2], _resolved[3],
                                    _resolved[4], _resolved[5]):
                    return _resolved[0], _resolved[1], _resolved_label, _ov_plane, _ov_display
                _cache_db_delete_route(callsign, 'resolved')
                _log(f"[resolved] {callsign}: cached {_resolved[0]}->{_resolved[1]} fails "
                     f"geometry check — busting stale entry, re-resolving")
            else:
                # No coordinates stored — bust so the fresh lookup stores them.
                _cache_db_delete_route(callsign, 'resolved')
                _log(f"[resolved] {callsign}: cached {_resolved[0]}->{_resolved[1]} has no "
                     f"coordinates — busting to re-resolve with plausibility data")

    # ── 0.75 VRS live route hint ───────────────────────────────────────────────
    # When the receiver is Virtual Radar Server, it may supply From/To airport
    # codes directly in the AircraftList feed.  Trust the hint if at least one
    # endpoint is a configured local airport — same rule as OpenSky free API.
    # Treated as a short-lived (1 hr) route cache entry so it doesn't interfere
    # with the 7-day resolved cache used for scheduled airlines.
    if vrs_origin and vrs_dest and not _override_partial:
        if _has_local_endpoint(vrs_origin, vrs_dest):
            _log(f"[vrs] {callsign}: {vrs_origin}->{vrs_dest} (local endpoint — accepted)")
            # VRS is live feed data — no DB cache write needed.  The route is
            # re-read from the ADS-B feed on every poll so persistence adds nothing,
            # and writing to cache_type='route' would collide with adsbdb's cache key.
            return vrs_origin, vrs_dest, "vrs", _ov_plane, _ov_display

    # ── N-number callsign → registration fallback ────────────────────────────
    # GA aircraft that don't transmit a registration in their ADS-B message
    # commonly broadcast their tail number as the Mode S callsign.  Promote it
    # here so §1 FR24 (and all registration-keyed cache lookups) use the right
    # value.  The same promotion happens in _grab_data() for display purposes,
    # but that runs AFTER get_route() returns — too late for §1.
    if not registration and callsign and _N_NUMBER_RE.match(callsign):
        registration = callsign.upper()

    # The ADS-B feed often omits the tail number; get_aircraft_type() caches a permanent
    # hex->reg mapping — fall back to it so BOTH FR24 paths see the tail.  This MUST run
    # BEFORE _is_n_number below: a GA/charter aircraft whose tail is only in the cache
    # (not the feed) would otherwise be read as non-GA and skip §1 entirely — e.g. a
    # NetJets EJMxxx flight whose tail N… is cached but whose callsign isn't an N-number.
    # A commercial flight's N-registration is NOT misrouted into §1, because §1 is also
    # gated on `not _adsbdb_commercial`.  (In no-cache test mode this read is bypassed;
    # run_test_lookup() resolves the tail from this same cache before enabling the bypass
    # and passes it in explicitly — keeping the diagnostic faithful to a real flight.)
    if not registration and hex_code:
        registration = _cache_db_get_reg(hex_code) or ""

    # Proactive OpenSky-metadata tail resolution for ANY flight still missing a tail — so
    # BOTH FR24 paths can run on the FIRST poll: §1 (GA / charter, by N-number reg) and §5
    # (commercial, by reg).  FR24's library has no callsign lookup, so a tail is the only
    # way to query it at all.  Permanent cache, backoff-guarded, no-op once known;
    # get_aircraft_type() resolves the same tail later this poll regardless, so this adds
    # no net API calls — it just makes the tail available before the FR24 stages run.
    if not registration and hex_code:
        _try_opensky_reg(hex_code)
        registration = _cache_db_get_reg(hex_code) or ""

    # Compute once — AFTER the tail is fully resolved, so _is_n_number reflects it (a
    # GA/charter aircraft whose tail arrived via cache or OpenSky must read as an N-number).
    _adsbdb_commercial = (bool(callsign) and len(callsign) >= 3
                          and callsign[:3].upper() in _SCHEDULED_PREFIXES)
    _is_n_number = bool(registration) and bool(_N_NUMBER_RE.match(registration))

    # ── 1. FlightRadar24 (GA / N-number — first resort) ──────────────────────
    # Queried first for N-number registrations — real-time, registration-based
    # lookup is more accurate than adsbdb's static historical routes for GA.
    # No local-origin restriction; registration uniquely identifies the aircraft
    # so through-traffic and arrivals are trustworthy.  No API key required.
    # Commercial callsigns are excluded here; FR24 serves them at §5 (last resort).
    # adsbdb / OpenSky still run after as fallbacks if FR24 has no data.
    _fr24_origin = _fr24_dest = ""
    _fr24_src = "fr24"
    if (not (origin and destination)
            and not _adsbdb_commercial
            and _is_n_number
            and _FR24_AVAILABLE
            and not os.path.exists(FR24_DISABLED_FLAG)):
        _cached_fr24 = _cache_db_get_route(f"fr24:{registration}", 'route')
        if _cached_fr24:
            _fr24_origin = _cached_fr24[0] or ""
            _fr24_dest   = _cached_fr24[1] or ""
            _fr24_src    = "fr24:cached"
        else:
            _fr24_f = None
            _fr24_active = []
            try:
                _fr24_api = _get_fr24_api()
                if _fr24_api is not None:
                    with _fr24_lock:   # serialize concurrent get_flights() calls
                        with warnings.catch_warnings():
                            warnings.simplefilter("ignore")   # suppress DeprecationWarning from shim
                            _fr24_flights = _fr24_api.get_flights(registration=registration) or []
                    # Prefer an airborne flight; fall back to the first result
                    _fr24_active = [f for f in _fr24_flights if _fr24_alt_int(f) > 0]
                    _fr24_f = _fr24_active[0] if _fr24_active else (_fr24_flights[0] if _fr24_flights else None)
                    if _fr24_f:
                        _fr24_origin = getattr(_fr24_f, 'origin_airport_iata', '') or ''
                        _fr24_dest   = getattr(_fr24_f, 'destination_airport_iata', '') or ''
                        # Side-effect: populate type cache for free while we have the Flight object
                        _fr24_ac_code = getattr(_fr24_f, 'aircraft_code', '') or ''
                        if _fr24_ac_code and hex_code:
                            _ac_cached = _cache_db_get_aircraft(hex_code)
                            if not (_ac_cached and _ac_cached[0]):
                                _cache_db_set_aircraft(hex_code, _translate_type(_fr24_ac_code),
                                                       "fr24", AIRCRAFT_CACHE_TTL)
                    if not (_fr24_origin or _fr24_dest):
                        _log(f"[fr24] {callsign} ({registration}): no route data")
            except Exception as e:
                _log(f"[fr24] {callsign} ({registration}): request error — {e}")
            # Unconditional cache write outside try — prevents per-poll retries on persistent failure
            _fr24_grounded = _fr24_f is not None and not _fr24_active  # result is grounded, no airborne alt
            # GA routes are keyed by registration (FR24 is reliable for a specific tail),
            # so a non-local GA route is usually CORRECT — keep it at the normal 1 h TTL
            # rather than dropping it (prefer local, but don't strip a valid non-local
            # route).  The read-side geometry check below still busts it if the plane
            # later moves off the route.  Only grounded/empty results get the short TTL.
            _fr24_ttl = (ROUTE_MISS_TTL if _fr24_grounded
                         else ROUTE_TTL_DEFAULT if (_fr24_origin or _fr24_dest)
                         else ROUTE_MISS_TTL)
            _cache_db_set_route(f"fr24:{registration}", 'route',
                                _fr24_origin, _fr24_dest,
                                None, None, None, None,
                                int(time.time()) + _fr24_ttl,
                                source="fr24")

        # Geometry parity: resolve the IATA codes to coords and reject a stale /
        # wrong-leg route before it can be committed — the same test the other
        # sources run.  Unknown airports fall through to benefit-of-the-doubt.
        if (_fr24_origin or _fr24_dest) and not _fr24_route_plausible(
                plane_lat, plane_lon, _fr24_origin, _fr24_dest):
            _cache_db_set_route(f"fr24:{registration}", 'route', '', '', None, None, None, None,
                                int(time.time()) + ROUTE_MISS_TTL, source="fr24")
            _log(f"[fr24] {registration}: {_route_display(_fr24_origin, _fr24_dest)} rejected — implausible route")
            _fr24_origin = _fr24_dest = ""

        if _fr24_origin or _fr24_dest:
            if _fr24_src != "fr24:cached":
                _log(f"[fr24] {registration}: {_route_display(_fr24_origin, _fr24_dest)} accepted")
            if not origin:
                origin = _fr24_origin
            if not destination:
                destination = _fr24_dest
            source = source or _fr24_src

    # ── 2. adsbdb (static historical DB) ──────────────────────────────────────
    adsbdb_origin = adsbdb_dest = ""
    adsbdb_olat = adsbdb_olon = adsbdb_dlat = adsbdb_dlon = None
    _adsbdb_src = "adsbdb"

    if callsign and not (origin and destination) and not os.path.exists(ADSBDB_DISABLED_FLAG):
        _cached_adsbdb = _cache_db_get_route(callsign, 'route')
        if _cached_adsbdb:
            adsbdb_origin, adsbdb_dest = _cached_adsbdb[0], _cached_adsbdb[1]
            adsbdb_olat, adsbdb_olon = _cached_adsbdb[2], _cached_adsbdb[3]
            adsbdb_dlat, adsbdb_dlon = _cached_adsbdb[4], _cached_adsbdb[5]
            _adsbdb_src = "adsbdb:cached"
        else:
            try:
                r = requests.get(ADSBDB_CALLSIGN_URL.format(callsign), timeout=5)
                if r.status_code == 200:
                    fr = (r.json().get("response") or {}).get("flightroute") or {}
                    _fr_orig      = fr.get("origin") or {}
                    _fr_dest      = fr.get("destination") or {}
                    adsbdb_origin = _fr_orig.get("iata_code", "") or ""
                    adsbdb_dest   = _fr_dest.get("iata_code", "") or ""
                    adsbdb_olat   = _fr_orig.get("latitude")
                    adsbdb_olon   = _fr_orig.get("longitude")
                    adsbdb_dlat   = _fr_dest.get("latitude")
                    adsbdb_dlon   = _fr_dest.get("longitude")
                    _remember_airport(adsbdb_origin, adsbdb_olat, adsbdb_olon)
                    _remember_airport(adsbdb_dest,   adsbdb_dlat, adsbdb_dlon)
                    # Cache the result.  Use ADSBDB_CACHE_TTL (1 hr) for both
                    # hits and misses — adsbdb is a static historical DB and a
                    # "no route" answer won't change between polls the way a
                    # real-time API might.  ROUTE_MISS_TTL (5 min) is only
                    # appropriate for live data sources.
                    _cache_db_set_route(callsign, 'route',
                                        adsbdb_origin, adsbdb_dest,
                                        adsbdb_olat, adsbdb_olon, adsbdb_dlat, adsbdb_dlon,
                                        int(time.time()) + ADSBDB_CACHE_TTL,
                                        source="adsbdb")
                elif r.status_code == 404:
                    # Callsign not in adsbdb's static DB — also cache for the
                    # full hour; the DB doesn't gain new entries between polls.
                    _cache_db_set_route(callsign, 'route',
                                        "", "", None, None, None, None,
                                        int(time.time()) + ADSBDB_CACHE_TTL,
                                        source="adsbdb")
                # 5xx / unexpected: don't cache — transient error, retry next poll
            except Exception as e:
                _log(f"[adsbdb] {callsign}: request error — {e}")

    # Trust adsbdb only for GA / non-commercial flights.
    # Scheduled airlines (callsign prefix in _SCHEDULED_PREFIXES) are NOT trusted from
    # adsbdb — its static historical DB maps to hex codes, and the same aircraft (hex)
    # flies different routes on different days, making historical data unreliable for
    # commercial ops.  The result is still cached to suppress repeat API calls, but
    # AirLabs / AeroAPI must be the authority — they run regardless and their answer wins.
    # For GA (N-numbers, non-scheduled prefixes) adsbdb remains trusted as before.
    # (_adsbdb_commercial already computed above, before the FR24 step.)
    _adsbdb_origin_local = adsbdb_origin.upper() in _LOCAL_AIRPORTS if adsbdb_origin else False
    adsbdb_ok = (
        not _adsbdb_commercial           # commercial flights require paid-API confirmation
        and _adsbdb_origin_local
        and _route_plausible(plane_lat, plane_lon,
                             adsbdb_olat, adsbdb_olon,
                             adsbdb_dlat, adsbdb_dlon)
    )
    if adsbdb_ok:
        # Fill blanks only — preserve any endpoint already set by a partial override.
        if not origin:
            origin = adsbdb_origin
        if not destination:
            destination = adsbdb_dest
        source = source or _adsbdb_src
        if _adsbdb_src != "adsbdb:cached":
            _log(f"[adsbdb] {_airline_display(callsign)}: {_route_display(adsbdb_origin, adsbdb_dest)} accepted")
    elif adsbdb_origin or adsbdb_dest:
        if _adsbdb_src != "adsbdb:cached":
            if _adsbdb_commercial:
                _log(f"[adsbdb] {callsign}: {adsbdb_origin or '?'}->{adsbdb_dest or '?'} not trusted — commercial; deferring to AirLabs/AeroAPI")
            elif not _adsbdb_origin_local:
                _log(f"[adsbdb] {callsign}: {adsbdb_origin or '?'}->{adsbdb_dest or '?'} skipped — origin not local")
            else:
                _log(f"[adsbdb] {callsign}: {adsbdb_origin or '?'}->{adsbdb_dest or '?'} skipped — plausibility failed")
    elif _adsbdb_src == "adsbdb":  # live call that returned nothing (not a cached negative)
        _log(f"[adsbdb] {callsign}: no data")

    # ── 2a. OpenSky by hex (free, unlimited — queried before AirLabs) ───────────
    # OpenSky doesn't return airport coordinates, so geometry plausibility isn't
    # possible.  Trusted only when the ORIGIN is a local airport (departing local).
    # If trusted, the destination is also accepted from the same result; a missing
    # dest falls through to AirLabs.  Arrival-only and through-traffic are skipped.
    #
    _sky_origin = _sky_dest = ""
    _sky_src = "opensky"
    # OpenSky is free/unlimited — intentionally excluded from the _apis_disabled kill-switch.
    if not (origin and destination) and OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET and hex_code and not _in_backoff("opensky") and not os.path.exists(OPENSKY_DISABLED_FLAG):
        _cached_sky = _cache_db_get_route(hex_code, 'route')
        if _cached_sky:
            _sky_origin, _sky_dest = _cached_sky[0], _cached_sky[1]
            _sky_src = "opensky:cached"
        else:
            token = _get_opensky_token()
            if token:
                try:
                    r = requests.get(
                        OPENSKY_FLIGHTS_URL,
                        params={"icao24": hex_code.lower(), "begin": now - OPENSKY_LOOKBACK_SECS, "end": now},
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=10,
                    )
                    if r.status_code == 429:
                        _set_backoff("opensky", secs=BACKOFF_RATE_LIMIT_SECS)
                    elif r.status_code in (401, 403):
                        _set_backoff("opensky", secs=BACKOFF_AUTH_SECS)
                        _log(f"[opensky] auth error ({r.status_code}) — check credentials")
                    elif r.status_code == 200:
                        sky_data = r.json()
                        if sky_data:
                            fl = max(sky_data, key=lambda f: f.get("firstSeen", 0))
                            _sky_origin = icao_to_iata(fl.get("estDepartureAirport") or "")
                            _sky_dest   = icao_to_iata(fl.get("estArrivalAirport") or "")
                        else:
                            _log(f"[opensky] {callsign}: no data")
                        # Cache hit or confirmed-empty 200 — suppresses re-queries within TTL.
                        # Non-200 status codes (5xx etc.) are not cached; retry next poll.
                        _sky_ttl = (ROUTE_MISS_TTL if _is_nonlocal(_sky_origin, _sky_dest)
                                    else OPENSKY_CACHE_TTL if (_sky_origin or _sky_dest)
                                    else ROUTE_MISS_TTL)
                        _cache_db_set_route(hex_code, 'route',
                                            _sky_origin, _sky_dest,
                                            None, None, None, None,
                                            int(time.time()) + _sky_ttl,
                                            source="opensky")
                except Exception as e:
                    _log(f"[opensky] {callsign}: request error — {e}")

        if _sky_origin or _sky_dest:
            # Only trust when the origin is a local airport (departing local).
            # For commercial callsigns, log what OpenSky found but don't commit —
            # AirLabs / AeroAPI are the authority (same policy as adsbdb above).
            _sky_origin_local = _sky_origin.upper() in _LOCAL_AIRPORTS if _sky_origin else False
            if _sky_origin_local:
                if _sky_src != "opensky:cached":
                    if _adsbdb_commercial:
                        _log(f"[opensky] {_airline_display(callsign)}: {_route_display(_sky_origin, _sky_dest)} found (commercial — deferring to AirLabs/AeroAPI)")
                    else:
                        _log(f"[opensky] {_airline_display(callsign)}: {_route_display(_sky_origin, _sky_dest)} accepted")
                if not _adsbdb_commercial:
                    # GA / non-scheduled: commit origin and destination
                    if not origin:
                        origin = _sky_origin
                    if _sky_dest and not destination:
                        destination = _sky_dest
                    source = source or _sky_src
                # Commercial: data is logged above but not committed — AirLabs/AeroAPI confirm
            elif _sky_src != "opensky:cached":
                _log(f"[opensky] {callsign}: {_sky_origin or '?'}->{_sky_dest or '?'} skipped — origin not local")

    # ── 2b. Free-API consensus (non-local departures / arrivals) ─────────────────
    # adsbdb keys by callsign; OpenSky keys by hex code — two independent KEYS.
    # If both return the exact same non-local route AND it passes the geometry
    # plausibility check (using adsbdb's airport coordinates), trust it without
    # burning a paid API call.  Both origin AND destination must agree; partial
    # matches fall through to AirLabs as before.
    #
    # LIMITATION (known, accepted): the two sources are key-diverse but NOT
    # provenance-independent — both reflect historical records for the SAME
    # airframe, so they can agree on a route the aircraft flew on a prior day.
    # The geometry check below is the primary guard against such stale agreement;
    # OpenSky's ~6-hour lookback window adds some recency on its side.  True source
    # independence isn't achievable from these two free feeds, so consensus is a
    # cost-saving optimisation, not gospel — the paid APIs still run whenever it
    # doesn't fire.
    if not (origin and destination) and not _adsbdb_commercial:
        if (adsbdb_origin and _sky_origin
                and adsbdb_origin.upper() == _sky_origin.upper()
                and adsbdb_dest and _sky_dest
                and adsbdb_dest.upper() == _sky_dest.upper()):
            _consensus_plausible = _route_plausible(
                plane_lat, plane_lon,
                adsbdb_olat, adsbdb_olon,
                adsbdb_dlat, adsbdb_dlon,
            )
            if _consensus_plausible:
                origin      = adsbdb_origin
                destination = adsbdb_dest
                # Mark as cached when both underlying sources were cache hits —
                # _is_live() uses this to skip the inter-flight rate-limit delay.
                _consensus_cached = (_adsbdb_src == "adsbdb:cached" and _sky_src == "opensky:cached")
                source      = "adsbdb+opensky:cached" if _consensus_cached else "adsbdb+opensky"
                _log(f"[route] {_airline_display(callsign)}: {_route_display(origin, destination)} accepted — free APIs agree")
            else:
                _log(f"[route] {callsign}: free APIs agree on {adsbdb_origin}->{adsbdb_dest} but route implausible — escalating to paid")

    # ── 2c. GA cross-check: adsbdb / OpenSky accuracy vs FR24 ───────────────────
    # For N-number aircraft, FR24 is treated as the ground truth.  When both FR24
    # AND a free API have data, we record whether they agreed — building accuracy
    # stats over time (visible on the Stats page).
    #
    # Guards:
    #   • Require a complete FR24 route (both origin AND destination) — partial FR24
    #     results would produce false mismatches against fully-resolved free API data.
    #   • Require FR24 to be a *live* call this invocation — cached FR24 data is not
    #     valid ground truth when comparing against a freshly-fetched free API result.
    _fr24_live = (_fr24_src == "fr24")   # True = live call this invocation
    if _is_n_number and _fr24_origin and _fr24_dest and _fr24_live:
        _fr24_route_str = f"{_fr24_origin}->{_fr24_dest}"

        if adsbdb_origin or adsbdb_dest:
            _ga_db_route = f"{adsbdb_origin or '?'}->{adsbdb_dest or '?'}"
            _ga_db_matched = (
                (adsbdb_origin or "").upper() == _fr24_origin.upper()
                and (adsbdb_dest or "").upper() == _fr24_dest.upper()
            )
            if _ga_db_matched:
                _log(f"[adsbdb] {callsign} ({registration}): FR24 confirmed adsbdb GA route ({_fr24_route_str})")
            else:
                _log(f"[adsbdb] {callsign} ({registration}): FR24 overrode adsbdb — was {_ga_db_route}, FR24 has {_fr24_route_str}")
            _record_ga_free_api_check(registration, callsign,
                                      "adsbdb", _ga_db_route, _fr24_route_str, _ga_db_matched)

        if _sky_origin or _sky_dest:
            _ga_sky_route = f"{_sky_origin or '?'}->{_sky_dest or '?'}"
            _ga_sky_matched = (
                (_sky_origin or "").upper() == _fr24_origin.upper()
                and (_sky_dest or "").upper() == _fr24_dest.upper()
            )
            if _ga_sky_matched:
                _log(f"[opensky] {callsign} ({registration}): FR24 confirmed OpenSky GA route ({_fr24_route_str})")
            else:
                _log(f"[opensky] {callsign} ({registration}): FR24 overrode OpenSky — was {_ga_sky_route}, FR24 has {_fr24_route_str}")
            _record_ga_free_api_check(registration, callsign,
                                      "opensky", _ga_sky_route, _fr24_route_str, _ga_sky_matched)

    # ── 3. AirLabs (real-time, 1,000 calls/month — now mainly through-traffic) ──
    # Only called when free APIs didn't resolve the route (no data, disagreement,
    # or implausible geometry).  Returns airport coordinates for plausibility check.
    # Skipped when the callsign is in _paid_miss_cache — both paid APIs already
    # confirmed empty within ROUTE_PAID_MISS_TTL (2 h), no point burning quota.
    _need_airlabs = not (origin and destination)
    # N-numbers, military, and callsigns that have hit both paid APIs with empty results
    # recently are all skipped — compute once up front to avoid re-evaluating inside guards.
    _skip_paid = _skip_paid_apis(callsign) or _override_partial or (
        bool(callsign) and _cache_db_check_paid_miss(callsign)
    )
    al_origin = al_dest = ""
    al_olat = al_olon = al_dlat = al_dlon = None
    _al_src   = "airlabs"
    _al_count = 0  # tracks live call count; > 0 means a real API call was made

    if _need_airlabs and AIRLABS_API_KEY and callsign and not _apis_disabled and not os.path.exists(AIRLABS_DISABLED_FLAG) and not _skip_paid:
        (al_origin, al_dest, al_olat, al_olon, al_dlat, al_dlon,
         _al_src, _al_count) = _query_airlabs(
            callsign, plane_lat, plane_lon,
            api_key=AIRLABS_API_KEY, cache_key=f"airlabs:{callsign}", backoff_name="airlabs",
            usage_file=AIRLABS_USAGE_FILE, reset_day=AIRLABS_RESET_DAY,
            monthly_limit=AIRLABS_MONTHLY_LIMIT, increment_fn=_airlabs_increment, log_tag="airlabs-1")

    # AirLabs returns coordinates — apply geometry plausibility only.
    # Paid APIs trust any plausible route (arrivals and through-traffic included);
    # origin-local restriction applies only to free APIs (adsbdb, OpenSky).
    _al1_cache_was_implausible = False
    if al_origin or al_dest:
        al_plausible = _route_plausible(plane_lat, plane_lon,
                                         al_olat, al_olon, al_dlat, al_dlon)
        if al_plausible:
            _al_is_nonlocal = _is_nonlocal(al_origin, al_dest)
            if _al_is_nonlocal:
                # Non-local route — do NOT commit to origin/dest yet.
                # Hold in al_origin/al_dest so the verification chain (AeroAPI, FR24)
                # can try to find a local route first.  The non-local result is used
                # as a fallback in the final-acceptance block at the end of get_route().
                _log(f"[airlabs-1] {callsign}: {_route_display(al_origin, al_dest)} non-local — deferring to AeroAPI/FR24 verification")
            else:
                _al_filled_dest = not destination
                # Require al_dest to be set before treating this as a dest-fill/conflict.
                # An origin-only partial (al_dest == "") must NOT enter here — otherwise
                # it clobbers an already-committed LOCAL origin with a non-local one and
                # mislabels it a "complete route"; it falls to the else (fill-blanks) path.
                if source and _al_filled_dest and al_dest:
                    if al_origin and origin and al_origin.upper() != origin.upper():
                        _log(f"[airlabs-1] origin conflict ({origin} vs {al_origin}) — preferring AirLabs-1 complete route")
                        origin = al_origin
                        destination = al_dest
                        source = _al_src
                    else:
                        if not origin:
                            origin = al_origin
                        destination = al_dest
                        source = f"{source}+airlabs"
                else:
                    if not origin:
                        origin = al_origin
                    if not destination:
                        destination = al_dest
                    source = source or _al_src
                if _al_src != "airlabs:cached":
                    _count_suffix = f" [call #{_al_count}]" if _al_count else ""
                    _log(f"[airlabs-1] {_airline_display(callsign)}: {_route_display(al_origin, al_dest)} accepted{_count_suffix}")
                if al_olat is not None:
                    _coord_olat, _coord_olon = al_olat, al_olon
                    _coord_dlat, _coord_dlon = al_dlat, al_dlon
                    _coord_origin_iata = origin
        else:
            if _al_src == "airlabs:cached":
                _cache_db_delete_route(f"airlabs:{callsign}", 'route')
                _al1_cache_was_implausible = True
            _log(f"[airlabs-1] implausible route {al_origin}->{al_dest} rejected for {callsign}")

    # ── 3b. AirLabs 2 (secondary key — fallback when AirLabs 1 has no usable result) ──
    # Tried when AirLabs 1 either:
    #   • made no live call (_al_count == 0): in-backoff, disabled, not configured, cache-hit
    #   • made a live call but is over quota (_al_count >= AIRLABS_MONTHLY_LIMIT):
    #     key 1 is exhausted but key 2 has its own separate quota and can still respond.
    # Skipped when AirLabs 1 made a live call and returned empty within quota
    # (both keys share the same backend data — key 2 would return the same empty result).
    al2_origin = al2_dest = ""
    al2_olat = al2_olon = al2_dlat = al2_dlon = None
    _al2_src   = "airlabs2"
    _al2_count = 0

    _al1_over_quota = _al_count >= AIRLABS_MONTHLY_LIMIT
    _al1_cache_was_empty = (_al_src == "airlabs:cached" and not al_origin and not al_dest)
    # Skip AL-2 whenever AL-1's cache has a non-local result.
    # Both keys share the same backend — AL-2 would return the same data.
    # With the new system, non-local routes are only cached at ROUTE_MISS_TTL (5 min)
    # so a cached non-local result is always fresh and trustworthy; no need to verify.
    _al1_cache_was_nonlocal = (
        _al_src == "airlabs:cached"
        and _is_nonlocal(al_origin, al_dest)
    )
    if (not (origin and destination)
            and (_al1_over_quota
                 or (_al_count == 0
                     and not _al1_cache_was_empty
                     and not _al1_cache_was_implausible
                     and not _al1_cache_was_nonlocal))
            and AIRLABS_API_KEY_2 and callsign
            and not _apis_disabled and not os.path.exists(AIRLABS2_DISABLED_FLAG)
            and not _skip_paid):
        (al2_origin, al2_dest, al2_olat, al2_olon, al2_dlat, al2_dlon,
         _al2_src, _al2_count) = _query_airlabs(
            callsign, plane_lat, plane_lon,
            api_key=AIRLABS_API_KEY_2, cache_key=f"airlabs2:{callsign}", backoff_name="airlabs2",
            usage_file=AIRLABS2_USAGE_FILE, reset_day=AIRLABS2_RESET_DAY,
            monthly_limit=AIRLABS2_MONTHLY_LIMIT, increment_fn=_airlabs2_increment, log_tag="airlabs-2")

    if al2_origin or al2_dest:
        al2_plausible = _route_plausible(plane_lat, plane_lon,
                                          al2_olat, al2_olon, al2_dlat, al2_dlon)
        if al2_plausible:
            _al2_is_nonlocal = _is_nonlocal(al2_origin, al2_dest)
            if _al2_is_nonlocal:
                _log(f"[airlabs-2] {callsign}: {_route_display(al2_origin, al2_dest)} non-local — deferring to AeroAPI/FR24 verification")
            else:
                _al2_filled_dest = not destination
                if source and _al2_filled_dest and al2_dest:  # al2_dest guard — see AirLabs-1
                    if al2_origin and origin and al2_origin.upper() != origin.upper():
                        _log(f"[airlabs-2] origin conflict ({origin} vs {al2_origin}) — preferring AirLabs-2 complete route")
                        origin = al2_origin
                        destination = al2_dest
                        source = _al2_src
                    else:
                        if not origin:
                            origin = al2_origin
                        destination = al2_dest
                        source = f"{source}+airlabs2"
                else:
                    if not origin:
                        origin = al2_origin
                    if not destination:
                        destination = al2_dest
                    source = source or _al2_src
                if _al2_src != "airlabs2:cached":
                    _count_suffix = f" [call #{_al2_count}]" if _al2_count else ""
                    _log(f"[airlabs-2] {_airline_display(callsign)}: {_route_display(al2_origin, al2_dest)} accepted{_count_suffix}")
                if al2_olat is not None:
                    _coord_olat, _coord_olon = al2_olat, al2_olon
                    _coord_dlat, _coord_dlon = al2_dlat, al2_dlon
                    _coord_origin_iata = origin
        else:
            if _al2_src == "airlabs2:cached":
                _cache_db_delete_route(f"airlabs2:{callsign}", 'route')
            _log(f"[airlabs-2] implausible route {al2_origin}->{al2_dest} rejected for {callsign}")

    # ── 4. FlightAware AeroAPI (paid — last resort, capped at monthly limit) ───
    # Also runs when AirLabs returned a complete non-local route — AeroAPI is
    # given a chance to supply a local-airport route instead.
    # _al_held_nonlocal: True when AirLabs-1 or AirLabs-2 had a complete non-local
    # route that was intentionally NOT committed to origin/destination (deferred for
    # AeroAPI/FR24 verification — origin/dest are still empty in that path).
    _al_held_nonlocal = (
        _is_nonlocal(al_origin, al_dest)
        or _is_nonlocal(al2_origin, al2_dest)
    )
    # Pre-initialize AeroAPI held-route vars at function scope.  They are otherwise
    # assigned only inside the live-200 branch below, yet the final-acceptance block
    # references them on every path.  Without this, any path that skips the AeroAPI
    # live call (no key, cache hit, backoff, non-200, _skip_paid) raises NameError.
    fa_origin = fa_dest = ""
    if (not (origin and destination) or _al_held_nonlocal) and FLIGHTAWARE_API_KEY and callsign and not _apis_disabled and not os.path.exists(AEROAPI_DISABLED_FLAG) and not _skip_paid:
        _cached_fa = _cache_db_get_route(callsign, 'aeroapi')
        if _cached_fa:
            # Apply geometry plausibility even on cache hits — a 7-day-old AeroAPI
            # result for the same callsign could be from a different prior flight leg.
            _cached_plausible = not _cached_fa[0] or _route_plausible(
                plane_lat, plane_lon, _cached_fa[2], _cached_fa[3],
                _cached_fa[4], _cached_fa[5]
            )
            if _cached_plausible:
                _fa_cached_origin = _cached_fa[0] or ""
                _fa_cached_dest   = _cached_fa[1] or ""
                if (_al_held_nonlocal
                        and _fa_cached_origin and _fa_cached_dest
                        and _has_local_endpoint(_fa_cached_origin, _fa_cached_dest)):
                    # AirLabs gave a complete non-local route; AeroAPI cache has a
                    # local-airport route — prefer it.
                    if al2_origin and al2_dest:
                        _al_nl_o, _al_nl_d = al2_origin, al2_dest
                    else:
                        _al_nl_o, _al_nl_d = al_origin, al_dest
                    _log(f"[aeroapi] {callsign}: AirLabs {_route_display(_al_nl_o, _al_nl_d)} non-local "
                         f"— preferring AeroAPI local route {_route_display(_fa_cached_origin, _fa_cached_dest)}")
                    origin      = _fa_cached_origin
                    destination = _fa_cached_dest
                    source = "aeroapi:cached"
                else:
                    _fa_cached_nonlocal = _is_nonlocal(_fa_cached_origin, _fa_cached_dest)
                    if not _fa_cached_nonlocal:
                        if not origin:
                            origin = _fa_cached_origin
                        if not destination:
                            destination = _fa_cached_dest
                        source = source or "aeroapi:cached"
                    else:
                        # Non-local cached AeroAPI route — surface it through the live
                        # held vars (fa_origin/fa_dest) so the last-resort picker can
                        # serve it, mirroring the live-call path.  (Without this a
                        # cached non-local AeroAPI result was silently dropped.)
                        fa_origin, fa_dest = _fa_cached_origin, _fa_cached_dest
                # Capture coords for the resolved-cache plausibility check.
                # Use _fa_cached_origin (the AeroAPI-returned airport code) as the iata
                # tag — NOT the 'origin' variable, which may be set by an earlier
                # source (e.g. AirLabs).  The resolved-cache write guard checks that
                # _coord_origin_iata == origin; a mismatch blocks a bad write.
                if _cached_fa[2] is not None:
                    _coord_olat, _coord_olon = _cached_fa[2], _cached_fa[3]
                    _coord_dlat, _coord_dlon = _cached_fa[4], _cached_fa[5]
                    _coord_origin_iata = _fa_cached_origin  # the airport these coords actually belong to
            else:
                _cache_db_delete_route(callsign, 'aeroapi')
                _log(f"[aeroapi] {callsign}: cached {_cached_fa[0]}->{_cached_fa[1]} fails geometry check — busting stale entry")
                _cached_fa = None  # signal that live call should fire this same poll
            # No log for normal cache hits — suppress spam; live fetches log below
        # Restore a persisted over-budget probe backoff (anchored across restarts) and
        # clear it at the billing reset — same daily-probe model as the AirLabs keys.
        _check_period_reset("aeroapi", AEROAPI_RESET_DAY)
        _restore_persisted_backoff("aeroapi", AEROAPI_USAGE_FILE, AEROAPI_RESET_DAY)
        if not _cached_fa and not _in_backoff("aeroapi"):
            try:
                r = requests.get(
                    AEROAPI_URL.format(callsign.strip()),
                    headers={"x-apikey": FLIGHTAWARE_API_KEY},
                    timeout=10,
                )
                if r.status_code == 429:
                    _set_backoff("aeroapi", secs=BACKOFF_RATE_LIMIT_SECS)
                elif r.status_code == 402:
                    # Payment required — over budget.  A rejected 402 isn't charged, so
                    # back off ONE probe interval and re-test daily (budget frees at the
                    # billing reset); persisted so restarts don't drift it — same model
                    # as the AirLabs keys.
                    _set_quota_backoff("aeroapi", AEROAPI_USAGE_FILE, AEROAPI_RESET_DAY,
                                       "aeroapi", f"{callsign}: 402 — over budget / no credit remaining")
                elif r.status_code in (401, 403):
                    _set_backoff("aeroapi", secs=BACKOFF_AUTH_SECS)
                    _log(f"[aeroapi] auth error ({r.status_code}) — check FLIGHTAWARE_API_KEY")
                elif r.status_code == 200:
                    flights = r.json().get("flights", [])
                    # Prefer an en-route flight; fall back to most recent
                    active = [f for f in flights if not f.get("actual_on")]
                    active.sort(key=lambda f: f.get("scheduled_out") or f.get("actual_off") or "", reverse=True)
                    f = active[0] if active else (flights[0] if flights else None)
                    fa_origin = fa_dest = ""
                    fa_olat = fa_olon = fa_dlat = fa_dlon = None
                    if f:
                        _fa_orig  = f.get("origin") or {}
                        _fa_dest  = f.get("destination") or {}
                        fa_origin = _fa_orig.get("code_iata", "") or ""
                        fa_dest   = _fa_dest.get("code_iata", "") or ""
                        fa_olat   = _fa_orig.get("latitude")
                        fa_olon   = _fa_orig.get("longitude")
                        fa_dlat   = _fa_dest.get("latitude")
                        fa_dlon   = _fa_dest.get("longitude")
                        _remember_airport(fa_origin, fa_olat, fa_olon)
                        _remember_airport(fa_dest,   fa_dlat, fa_dlon)
                    _aeroapi_increment()  # logs running spend
                    if not (fa_origin or fa_dest):
                        _log(f"[aeroapi] {callsign}: no flights returned")
                    # When checking for a local-route alternative (_al_held_nonlocal),
                    # use ROUTE_TTL_DEFAULT (1 hr) for empty results instead of ROUTE_MISS_TTL
                    # (5 min) — the flight won't gain a local endpoint mid-flight, so
                    # re-querying every 5 min would burn AeroAPI quota needlessly.
                    _fa_miss_ttl = ROUTE_TTL_DEFAULT if _al_held_nonlocal else ROUTE_MISS_TTL
                    _fa_is_nonlocal_cache = _is_nonlocal(fa_origin, fa_dest)
                    # Non-local complete routes → ROUTE_MISS_TTL (5 min); they're almost
                    # certainly wrong for this zone and must not persist.
                    # Local or partial results → ROUTE_TTL_DEFAULT (1 hr).
                    _fa_ttl = (
                        ROUTE_MISS_TTL if _fa_is_nonlocal_cache
                        else ROUTE_TTL_DEFAULT if (fa_origin or fa_dest)
                        else _fa_miss_ttl
                    )
                    _cache_db_set_route(callsign, 'aeroapi',
                                        fa_origin, fa_dest,
                                        fa_olat, fa_olon, fa_dlat, fa_dlon,
                                        int(time.time()) + _fa_ttl,
                                        source="aeroapi")
                    if fa_origin or fa_dest:
                        fa_plausible = _route_plausible(plane_lat, plane_lon,
                                                         fa_olat, fa_olon,
                                                         fa_dlat, fa_dlon)
                        if fa_plausible:
                            # Paid APIs trust any plausible route — no origin-local restriction.
                            _fa_applied   = False   # tracks whether AeroAPI route was actually adopted
                            _fa_filled_dest = not destination
                            # Compute non-local status once, before the branch split.  A
                            # non-local AeroAPI route (neither endpoint local) is NEVER
                            # committed to origin/destination — it is held for FR24 §5 /
                            # final-acceptance instead (the no-cache-non-local rule).
                            _fa_is_nonlocal = _is_nonlocal(fa_origin, fa_dest)
                            if source and _fa_filled_dest and fa_dest:  # fa_dest guard — see AirLabs-1
                                # A prior source set origin only; AeroAPI fills the missing
                                # destination.  Only commit when the AeroAPI route keeps a
                                # local endpoint — otherwise defer (never commit non-local).
                                if _fa_is_nonlocal:
                                    # AeroAPI's complete route is non-local — don't overwrite
                                    # the prior origin.  Hold for FR24 / final-acceptance.
                                    _log(f"[aeroapi] {callsign}: {_route_display(fa_origin, fa_dest)} non-local — deferring to FR24")
                                elif fa_origin and origin and fa_origin.upper() != origin.upper():
                                    _log(f"[aeroapi] origin conflict ({origin} vs {fa_origin}) — preferring AeroAPI complete route")
                                    origin = fa_origin
                                    destination = fa_dest
                                    source = "aeroapi"
                                    _fa_applied = True
                                else:
                                    if not origin:
                                        origin = fa_origin
                                    destination = fa_dest
                                    source = f"{source}+aeroapi"
                                    _fa_applied = True
                            else:
                                if (_al_held_nonlocal
                                        and fa_origin and fa_dest
                                        and _has_local_endpoint(fa_origin, fa_dest)):
                                    # AirLabs returned a complete non-local route; AeroAPI
                                    # has a local-airport route — prefer it.
                                    origin      = fa_origin
                                    destination = fa_dest
                                    source = "aeroapi"
                                    _fa_applied = True
                                else:
                                    if _fa_is_nonlocal:
                                        # Non-local AeroAPI result — don't commit; hold for
                                        # FR24 §5 to try, then final-acceptance picks best.
                                        _log(f"[aeroapi] {callsign}: {_route_display(fa_origin, fa_dest)} non-local — deferring to FR24")
                                    else:
                                        if not origin:
                                            origin = fa_origin
                                            _fa_applied = True
                                        if not destination:
                                            destination = fa_dest
                                            _fa_applied = True
                                        source = source or "aeroapi"
                            if _fa_applied:
                                _log(f"[aeroapi] {_airline_display(callsign)}: {_route_display(fa_origin, fa_dest)} accepted")
                            # Capture coords for the resolved-cache plausibility check.
                            # Use fa_origin (the AeroAPI-returned airport code) as the
                            # iata tag — NOT the 'origin' variable, which may be from
                            # an earlier source (e.g. AirLabs).
                            if fa_olat is not None:
                                _coord_olat, _coord_olon = fa_olat, fa_olon
                                _coord_dlat, _coord_dlon = fa_dlat, fa_dlon
                                _coord_origin_iata = fa_origin  # the airport these coords actually belong to
                        else:
                            _log(f"[aeroapi] {callsign}: {fa_origin}->{fa_dest} rejected — implausible route")
                            # Overwrite the route just cached above with a short negative
                            # entry.  Otherwise the next poll re-reads it, the geometry
                            # check busts+deletes it, and a fresh AeroAPI call fires again
                            # (AeroAPI returns the same leg for minutes) — re-billing on a
                            # loop.  The 5-min negative entry rate-limits the refetch; the
                            # route self-heals on the next expiry.
                            _cache_db_set_route(callsign, 'aeroapi', "", "", None, None, None, None,
                                                int(time.time()) + ROUTE_MISS_TTL, source="aeroapi")
                else:
                    # Unexpected status (4xx/5xx other than explicitly handled codes) —
                    # negatively cache for ROUTE_MISS_TTL to prevent per-poll retries.
                    _log(f"[aeroapi] unexpected status {r.status_code} for {callsign} — negative caching")
                    _cache_db_set_route(callsign, 'aeroapi',
                                        "", "", None, None, None, None,
                                        int(time.time()) + ROUTE_MISS_TTL,
                                        source="aeroapi")
            except Exception as e:
                _log(f"[aeroapi] {callsign}: request error — {e}")
        # else: in backoff — already logged when backoff was set

    origin      = origin      if origin.upper()      not in BLANK_FIELDS else ""
    destination = destination if destination.upper() not in BLANK_FIELDS else ""

    # ── 5. FlightRadar24 (commercial — free, last resort after all paid APIs) ──
    # Reached only when AirLabs and AeroAPI both returned no route for a commercial
    # callsign.  Uses registration-based lookup (same as GA §1) — the FR24 library
    # does not support a callsign filter parameter.  Cached by callsign so the same
    # scheduled flight is recognised across polls.
    # _all_paid_nonlocal: all paid APIs (AirLabs + AeroAPI) held non-local results;
    # give FR24 a chance to return a local route before final acceptance.
    _all_paid_nonlocal = _al_held_nonlocal and not (origin and destination)
    # Pre-initialize FR24-commercial held-route vars at function scope — they are
    # otherwise assigned only inside the block below, but the final-acceptance block
    # references them.  Defends against NameError on any path that skips FR24 §5.
    _fr24_com_origin = _fr24_com_dest = ""
    # Free — not gated by _apis_disabled or _skip_paid; only the per-API flag applies.
    if ((not (origin and destination) or _all_paid_nonlocal)
            and _adsbdb_commercial
            and _FR24_AVAILABLE
            and registration
            and callsign
            and not os.path.exists(FR24_DISABLED_FLAG)):
        _cached_fr24_com = _cache_db_get_route(f"fr24:{callsign}", 'route')
        if _cached_fr24_com:
            _fr24_com_origin = _cached_fr24_com[0] or ""
            _fr24_com_dest   = _cached_fr24_com[1] or ""
            _fr24_com_src    = "fr24:cached"
        else:
            _fr24_com_origin = _fr24_com_dest = ""
            _fr24_com_src    = "fr24"
            try:
                _fr24_api = _get_fr24_api()
                if _fr24_api is not None:
                    with _fr24_lock:   # serialize concurrent get_flights() calls
                        with warnings.catch_warnings():
                            warnings.simplefilter("ignore")
                            _fr24_com_flights = _fr24_api.get_flights(registration=registration) or []
                    _fr24_com_active = [f for f in _fr24_com_flights if _fr24_alt_int(f) > 0]
                    _fr24_com_f = (_fr24_com_active[0] if _fr24_com_active
                                   else (_fr24_com_flights[0] if _fr24_com_flights else None))
                    if _fr24_com_f:
                        _fr24_com_origin = getattr(_fr24_com_f, 'origin_airport_iata', '') or ''
                        _fr24_com_dest   = getattr(_fr24_com_f, 'destination_airport_iata', '') or ''
                        # Side-effect: populate type cache for free while we have the Flight object
                        _fr24_com_ac_code = getattr(_fr24_com_f, 'aircraft_code', '') or ''
                        if _fr24_com_ac_code and hex_code:
                            _ac_cached = _cache_db_get_aircraft(hex_code)
                            if not (_ac_cached and _ac_cached[0]):
                                _cache_db_set_aircraft(hex_code, _translate_type(_fr24_com_ac_code),
                                                       "fr24", AIRCRAFT_CACHE_TTL)
                    if not (_fr24_com_origin or _fr24_com_dest):
                        _log(f"[fr24] {callsign}: no route data")
            except Exception as e:
                _log(f"[fr24] {callsign}: request error — {e}")
            # Cache the result unconditionally — even when _fr24_api is None or an
            # exception fired, writing the (empty) miss entry prevents every subsequent
            # poll from re-entering this block and attempting the API call again.
            _fr24_com_is_nonlocal_cache = _is_nonlocal(_fr24_com_origin, _fr24_com_dest)
            _fr24_com_ttl = (
                ROUTE_MISS_TTL if _fr24_com_is_nonlocal_cache
                else ROUTE_TTL_DEFAULT if (_fr24_com_origin or _fr24_com_dest)
                else ROUTE_MISS_TTL
            )
            _cache_db_set_route(f"fr24:{callsign}", 'route',
                                _fr24_com_origin, _fr24_com_dest,
                                None, None, None, None,
                                int(time.time()) + _fr24_com_ttl,
                                source="fr24")

        # Geometry parity (same as §1): reject a stale/wrong-leg FR24 route before it
        # can be committed.  Unknown airports fall through to benefit-of-the-doubt.
        if (_fr24_com_origin or _fr24_com_dest) and not _fr24_route_plausible(
                plane_lat, plane_lon, _fr24_com_origin, _fr24_com_dest):
            _cache_db_set_route(f"fr24:{callsign}", 'route', '', '', None, None, None, None,
                                int(time.time()) + ROUTE_MISS_TTL, source="fr24")
            _log(f"[fr24] {_airline_display(callsign)}: "
                 f"{_route_display(_fr24_com_origin, _fr24_com_dest)} rejected — implausible route")
            _fr24_com_origin = _fr24_com_dest = ""

        if _fr24_com_origin or _fr24_com_dest:
            _fr24_com_is_nonlocal = _is_nonlocal(_fr24_com_origin, _fr24_com_dest)
            if _all_paid_nonlocal and not _fr24_com_is_nonlocal:
                # All paid APIs were non-local; FR24 returned a local route — use it.
                _log(f"[fr24] {_airline_display(callsign)}: "
                     f"{_route_display(_fr24_com_origin, _fr24_com_dest)} local — overriding non-local paid results")
                origin      = _fr24_com_origin
                destination = _fr24_com_dest
                source      = _fr24_com_src
            elif not _fr24_com_is_nonlocal:
                # Normal fill-blanks path (not all-paid-nonlocal context).
                if _fr24_com_src != "fr24:cached":
                    _log(f"[fr24] {_airline_display(callsign)}: "
                         f"{_route_display(_fr24_com_origin, _fr24_com_dest)} accepted (commercial)")
                if not origin:
                    origin = _fr24_com_origin
                if not destination:
                    destination = _fr24_com_dest
                source = source or _fr24_com_src
            else:
                # FR24 also non-local — hold for final acceptance.
                if _fr24_com_src != "fr24:cached":
                    _log(f"[fr24] {_airline_display(callsign)}: "
                         f"{_route_display(_fr24_com_origin, _fr24_com_dest)} non-local — held for final acceptance")

    # ── Last-resort selection: walk the full hierarchy for any usable route ────
    # Local routes already committed inline and short-circuited.  If we're still
    # here, NO trusted source returned a local route — so continue down the
    # hierarchy (AirLabs → AeroAPI → FR24 → adsbdb/OpenSky) and pick from everything
    # that was deferred: the trusted held non-local routes AND the free historical
    # DBs.  Candidates are re-validated for geometry, then ranked by
    # (tier, SOURCE_PRIORITY):
    #   • tier: local-complete < local-partial < non-local-complete < non-local-partial
    #     — a LOCAL endpoint outranks completeness, so a free LOCAL route beats a
    #     trusted NON-LOCAL one (prefer-local intent);
    #   • within a tier, SOURCE_PRIORITY orders airlabs > aeroapi > fr24 > free.
    # An inline-committed LOCAL partial is only overridden by a strictly-better tier.
    # Reached only as a last resort; result is short-TTL and never written to the
    # resolved cache (no coordinates captured), so an unreliable guess can't persist.
    if not (origin and destination):
        # Candidates include PARTIALS (origin OR dest) so a LOCAL partial — e.g.
        # OpenSky finding only the departure airport (LAS->?) — competes against and,
        # via the tier sort below, BEATS a stale non-local complete (e.g. a previous
        # leg SYR->CHS) returned by another source.  Geometry filtering still applies.
        _cands = []
        if al_origin or al_dest:
            _cands.append(("airlabs", al_origin, al_dest))
        if al2_origin or al2_dest:
            _cands.append(("airlabs2", al2_origin, al2_dest))
        if fa_origin or fa_dest:
            _cands.append(("aeroapi", fa_origin, fa_dest))
        if _fr24_com_origin or _fr24_com_dest:
            _cands.append(("fr24", _fr24_com_origin, _fr24_com_dest))
        # Consensus of the two free feeds (both COMPLETE and agreeing) is the
        # strongest free signal; for commercial these were recorded but deferred.
        if (adsbdb_origin and _sky_origin and adsbdb_origin.upper() == _sky_origin.upper()
                and adsbdb_dest and _sky_dest and adsbdb_dest.upper() == _sky_dest.upper()):
            _cands.append(("adsbdb+opensky", adsbdb_origin, adsbdb_dest))
        if adsbdb_origin or adsbdb_dest:
            _cands.append(("adsbdb", adsbdb_origin, adsbdb_dest))
        if _sky_origin or _sky_dest:
            _cands.append(("opensky", _sky_origin, _sky_dest))
        # Re-validate geometry here.  A route an inline check already rejected as
        # implausible leaves its held vars set, so without this filter the picker
        # could resurrect and serve it — exactly the stale/wrong-leg ("random PHX")
        # routes the system exists to reject.  Validation uses the harvested
        # airport-coords table; airports it doesn't know get benefit of the doubt,
        # matching each source's own inline coordless behavior.
        _cands = [c for c in _cands
                  if _fr24_route_plausible(plane_lat, plane_lon, c[1], c[2])]
        if _cands:
            # Tier: 0 local-complete, 1 local-partial, 2 non-local-complete,
            # 3 non-local-partial.  A LOCAL endpoint outranks completeness (your
            # "1. local") so a known home endpoint beats a probably-wrong non-local
            # route; within a locality, a complete route beats a partial one.
            def _route_tier(o, d):
                return (0 if _has_local_endpoint(o, d) else 2) + (0 if (o and d) else 1)
            _cands.sort(key=lambda c: (_route_tier(c[1], c[2]), SOURCE_PRIORITY.get(c[0], 99)))
            _best = _cands[0]
            # Only override the inline-committed state when the candidate is a
            # STRICTLY better tier — so a held NON-LOCAL complete cannot clobber an
            # already-committed LOCAL partial (a known home endpoint beats a non-local
            # guess).  A LOCAL complete still upgrades a local partial.
            _cur_tier = _route_tier(origin, destination) if (origin or destination) else 99
            if _route_tier(_best[1], _best[2]) < _cur_tier:
                _tier_label = ("local" if _has_local_endpoint(_best[1], _best[2])
                               else "non-local (no local found anywhere)")
                origin, destination = _best[1], _best[2]
                # Preserve the :cached marker the picker otherwise drops — a cached
                # last-resort route was served under a bare source name and looked live
                # (e.g. a cached OpenSky NV98->? read as [route:opensky], not :cached).
                _bs = _best[0]
                if   _bs == "airlabs":  source = _al_src
                elif _bs == "airlabs2": source = _al2_src
                elif _bs == "aeroapi":  source = "aeroapi:cached" if _cached_fa else "aeroapi"
                elif _bs == "fr24":     source = _fr24_com_src
                elif _bs == "adsbdb":   source = _adsbdb_src
                elif _bs == "opensky":  source = _sky_src
                elif _bs == "adsbdb+opensky":
                    source = ("adsbdb+opensky:cached"
                              if _adsbdb_src.endswith(":cached") and _sky_src.endswith(":cached")
                              else "adsbdb+opensky")
                else:                   source = _bs
                _log(f"[route] {_airline_display(callsign)}: no committed local route — "
                     f"serving {_tier_label} {_route_display(origin, destination)} from {source} "
                     f"(short-TTL; not written to resolved cache)")


    # If still no route and all eligible paid APIs returned empty, record a combined
    # miss so none are called again for ROUTE_PAID_MISS_TTL.
    # The AirLabs "chain" (key 1 or key 2) counts as one paid tier — if at least one
    # was eligible AND AeroAPI was eligible, the full paid stack has been exhausted.
    _airlabs_was_eligible = (
        bool(AIRLABS_API_KEY) and not _apis_disabled
        and not os.path.exists(AIRLABS_DISABLED_FLAG)
        and not _skip_paid and not _in_backoff("airlabs")
    )
    _airlabs2_was_eligible = (
        bool(AIRLABS_API_KEY_2) and not _apis_disabled
        and not os.path.exists(AIRLABS2_DISABLED_FLAG)
        and not _skip_paid and not _in_backoff("airlabs2")
    )
    _aeroapi_was_eligible = (
        bool(FLIGHTAWARE_API_KEY) and not _apis_disabled
        and not os.path.exists(AEROAPI_DISABLED_FLAG)
        and not _skip_paid and not _in_backoff("aeroapi")
    )
    # Fires when the PAID chain (AirLabs-1, AirLabs-2, AeroAPI) returned nothing —
    # independent of whether the free safety net (FR24, adsbdb, OpenSky) later filled
    # the route — so the paid APIs are suppressed for 2 h when they genuinely had no
    # data.  FR24 is FREE and is intentionally NOT included: its having a route must
    # not keep the (empty) paid APIs from being suppressed, or they re-bill every poll.
    _paid_returned_nothing = not (al_origin or al_dest or al2_origin or al2_dest
                                  or fa_origin or fa_dest)
    # Only record a paid-miss when the paid chain was actually CONSULTED (the route was
    # still incomplete at AirLabs' turn) — not when a cheaper source (e.g. FR24 §1)
    # resolved it first and the paid APIs were short-circuited.  Otherwise we'd suppress
    # paid for a flight we never asked them about and log a misleading "paid returned empty".
    if (_paid_returned_nothing and _need_airlabs and callsign
            and (_airlabs_was_eligible or _airlabs2_was_eligible)
            and _aeroapi_was_eligible):
        _cache_db_set_paid_miss(callsign)
        _log(f"[route] {callsign}: all paid APIs returned empty — suppressing for {ROUTE_PAID_MISS_TTL // 3600}h")

    # Cross-check log — for commercial flights where adsbdb had data, report whether
    # the paid APIs confirmed or overrode it (GA flights never enter this branch).
    # Skip when FR24 was the source — it's free, not a paid API, and the log/stats
    # would misleadingly attribute its result to AirLabs/AeroAPI.
    if (_adsbdb_commercial and (adsbdb_origin or adsbdb_dest) and (origin or destination)
            and "fr24" not in (source or "")
            and source not in ("adsbdb", "opensky", "adsbdb+opensky")):
        _db_route   = f"{adsbdb_origin or '?'}->{adsbdb_dest or '?'}"
        _paid_route = f"{origin or '?'}->{destination or '?'}"
        _matched    = ((adsbdb_origin or "").upper() == (origin or "").upper()
                       and (adsbdb_dest or "").upper() == (destination or "").upper())
        # Only log on the first (live) resolution — suppress on cached repeat polls
        # where the route is already known and the cross-check adds no new information.
        _source_is_cached = bool(source) and source.endswith(":cached")
        if not _source_is_cached:
            if _matched:
                _log(f"[adsbdb] {callsign}: paid APIs confirmed adsbdb route ({_paid_route})")
            else:
                _log(f"[adsbdb] {callsign}: paid APIs overrode adsbdb — was {_db_route}, now {_paid_route}")
        _record_free_api_check(callsign, _db_route, _paid_route, _matched)

    # ── _select() is the route authority (Phase-3 flip, now permanent) ──────────
    # The 4-day shadow soak proved _select()'s route matches the live inline pick on
    # every fresh resolution (1,437/0); the harness + a label-shadow pass confirmed the
    # source labels too, and the cleaner attribution (a complete source credited alone,
    # not combined with a redundant partial) was chosen deliberately.  A second live soak
    # (~660 resolutions, 0 disagreements) cleared the [flip-check] backstop, which has now
    # been retired.  _select() DRIVES the result: gather candidates, pick, reconstruct the
    # cached-aware label, commit.  The inline chain above still runs ONLY to (a) short-circuit
    # paid calls and (b) seed override-partial — it no longer decides the final route.
    if not _override_partial:
        _cands = []
        if _fr24_origin or _fr24_dest:
            _cands.append(_Cand(_fr24_origin, _fr24_dest, None, None, None, None, "fr24", _fr24_src == "fr24"))
        if al_origin or al_dest:
            _cands.append(_Cand(al_origin, al_dest, al_olat, al_olon, al_dlat, al_dlon, "airlabs", _al_count > 0))
        if al2_origin or al2_dest:
            _cands.append(_Cand(al2_origin, al2_dest, al2_olat, al2_olon, al2_dlat, al2_dlon, "airlabs2", _al2_count > 0))
        if fa_origin or fa_dest:
            # AeroAPI's per-call coord locals (fa_olat…) aren't function-scoped, so look the
            # endpoint coords back up from the harvested airport table (populated by AeroAPI's
            # own _remember_airport, live or cached).  Without real coords here the resolved-
            # cache write guard would skip AeroAPI-resolved ARRIVALS (non-local origin -> home
            # airport), silently re-billing the paid chain every poll.
            _fa_o = _airport_coords(fa_origin) or (None, None)
            _fa_d = _airport_coords(fa_dest) or (None, None)
            _cands.append(_Cand(fa_origin, fa_dest, _fa_o[0], _fa_o[1], _fa_d[0], _fa_d[1], "aeroapi", False))
        if _fr24_com_origin or _fr24_com_dest:
            _cands.append(_Cand(_fr24_com_origin, _fr24_com_dest, None, None, None, None, "fr24", _fr24_com_src == "fr24"))
        if (adsbdb_origin and _sky_origin and adsbdb_origin.upper() == _sky_origin.upper()
                and adsbdb_dest and _sky_dest and adsbdb_dest.upper() == _sky_dest.upper()):
            _cands.append(_Cand(adsbdb_origin, adsbdb_dest, adsbdb_olat, adsbdb_olon,
                                adsbdb_dlat, adsbdb_dlon, "adsbdb+opensky", False))
        if adsbdb_origin or adsbdb_dest:
            _cands.append(_Cand(adsbdb_origin, adsbdb_dest, adsbdb_olat, adsbdb_olon,
                                adsbdb_dlat, adsbdb_dlon, "adsbdb", _adsbdb_src == "adsbdb"))
        if _sky_origin or _sky_dest:
            _cands.append(_Cand(_sky_origin, _sky_dest, None, None, None, None, "opensky", _sky_src == "opensky"))
        _best = _select(_cands, plane_lat, plane_lon)

        # Reconstruct the cached-aware source label from _select's bare source name,
        # reusing the picker's bare->live mapping and handling the '+' merge combos.
        def _select_lbl1(p):
            if p == "airlabs":  return _al_src
            if p == "airlabs2": return _al2_src
            if p == "aeroapi":  return "aeroapi:cached" if _cached_fa else "aeroapi"
            if p == "fr24":     return _fr24_com_src if (_fr24_com_origin or _fr24_com_dest) else _fr24_src
            if p == "adsbdb":   return _adsbdb_src
            if p == "opensky":  return _sky_src
            return p
        def _select_label(s):
            if not s:
                return s
            if s == "adsbdb+opensky":
                return ("adsbdb+opensky:cached"
                        if _adsbdb_src.endswith(":cached") and _sky_src.endswith(":cached")
                        else "adsbdb+opensky")
            if "+" in s:
                _a, _b = s.split("+", 1)
                return f"{_select_lbl1(_a)}+{_select_lbl1(_b)}"
            return _select_lbl1(s)

        origin, destination = (_best.origin, _best.dest) if _best else ("", "")
        if _best:
            source = _select_label(_best.source)
            _coord_olat, _coord_olon = _best.olat, _best.olon
            _coord_dlat, _coord_dlon = _best.dlat, _best.dlon
            _coord_origin_iata = _best.origin

    # ── Resolved-route cache write ─────────────────────────────────────────────
    # If both endpoints are now known for a scheduled airline, cache the final
    # result so future sightings of the same daily flight skip the full API
    # chain — even when individual upstream caches (e.g. OpenSky's 1-hour TTL)
    # have expired or only have partial data.
    # Coords requirement is conditional on locality.  The READ trusts a LOCAL-origin entry
    # unconditionally (no geometry), so a local-origin route needs no coords — even a
    # coordless source (AirLabs without lat/lng, FR24) gets a 7-day entry that short-circuits
    # the whole chain on future sightings instead of re-walking adsbdb -> OpenSky -> AirLabs
    # every poll.  A NON-local origin's read DOES geometry-check, so a coordless non-local
    # entry would bust every poll (perpetual bust cycle) — those still require coords and
    # otherwise fall back to the per-API caches.
    if (origin and destination and callsign
            and _route_ttl(callsign) == ROUTE_TTL_SCHEDULED
            and _LOCAL_AIRPORTS                       # GLOBAL mode (no home) skips the 7-day cache
            and _has_local_endpoint(origin, destination)
            and (origin.upper() in _LOCAL_AIRPORTS or _coord_olat is not None)
            and (not _coord_origin_iata or _coord_origin_iata == origin)):  # guard mismatch
        _cache_db_set_route(callsign, 'resolved',
                            origin, destination,
                            _coord_olat, _coord_olon, _coord_dlat, _coord_dlon,
                            int(time.time()) + ROUTE_TTL_SCHEDULED,
                            source=source.removesuffix(":cached"))
    elif (origin and destination and callsign
            and _route_ttl(callsign) == ROUTE_TTL_SCHEDULED
            and _coord_olat is not None
            and _coord_origin_iata and _coord_origin_iata != origin):
        _log(f"[resolved] {callsign}: skipping resolved-cache write — coord origin "
             f"({_coord_origin_iata}) does not match final origin ({origin})")
    elif (origin and destination and callsign
            and _route_ttl(callsign) == ROUTE_TTL_SCHEDULED
            and not _has_local_endpoint(origin, destination)):
        _log(f"[resolved] {callsign}: skipping resolved-cache write — "
             f"non-local route {_route_display(origin, destination)} must not persist 7 days")
    elif (origin and destination and callsign
            and _route_ttl(callsign) == ROUTE_TTL_SCHEDULED
            and _LOCAL_AIRPORTS
            and origin.upper() in _LOCAL_AIRPORTS):
        # Diagnostic (Issue B): a scheduled, complete, LOCAL-ORIGIN departure should
        # always persist — condition 5 passes on locality and condition 6 passes unless
        # a coord-iata mismatch (caught by the first elif).  The resolved-write logic is
        # provably correct in tests for every modeled input, yet a few flights (AAL2040,
        # SCX3001) carried stale entries live.  If this ever fires it pinpoints the
        # real-world state we could not reproduce in mocks; logs the exact guard inputs.
        _log(f"[resolved] {callsign}: NOT persisted (unexpected) — "
             f"{_route_display(origin, destination)} src={source} "
             f"coord_olat={_coord_olat is not None} coord_iata={_coord_origin_iata!r}")

    # (The Phase-2 read-only shadow and the Phase-3 [flip-check] backstop that compared
    # _select() to the inline logic have both been retired now that _select() is the proven
    # sole authority above.  The inline per-source commits remain only for paid-call
    # short-circuiting and override-partial completion.)

    if _trace is not None:
        # diagnostic only: each source's raw result for the test-flight step grid
        _trace["adsbdb_route"] = {"origin": adsbdb_origin, "destination": adsbdb_dest}
        _trace["opensky"]      = {"origin": _sky_origin, "destination": _sky_dest}
        # Report whichever AirLabs key actually produced a result as a unit — never mix
        # key-1's origin with key-2's dest into a route neither key returned.
        _al_tr_o, _al_tr_d = (al_origin, al_dest) if (al_origin or al_dest) else (al2_origin, al2_dest)
        _trace["airlabs"]      = {"origin": _al_tr_o, "destination": _al_tr_d}
        _trace["aeroapi"]      = {"origin": fa_origin, "destination": fa_dest}

    # Drop non-IATA airport codes (e.g. OpenSky FAA identifiers like "NV98") to "?" — real
    # IATA codes are 3 letters and a 4-char code doesn't fit the display.  Done at the
    # boundary (after the shadow), so internal resolution/diagnostics keep the raw code.
    # If both endpoints drop out there's no usable route, so the source label becomes "none".
    origin, destination = _clean_iata(origin), _clean_iata(destination)
    if not (origin or destination):
        source = "none"
    return origin, destination, source or "none", _ov_plane, _ov_display


def _try_opensky_reg(hex_code: str) -> None:
    """
    Best-effort: call OpenSky metadata endpoint for registration only.
    Called when airplanes.live has type data but no 'r' field, and on cache hits
    where the type is known but no reg has been cached yet.
    Permanent hex→tail mapping — stops being called once reg is cached.
    No-op if reg is already cached, on error, 429, or global opensky_meta backoff.
    """
    if _in_backoff("opensky_meta"):
        return
    # Re-check the cache before the HTTP call — prevents duplicate in-flight requests
    # when two threads encounter the same aircraft simultaneously (both see no reg,
    # both enter this function; the second thread sees the reg the first just wrote).
    if _cache_db_get_reg(hex_code):
        return
    try:
        r = requests.get(OPENSKY_AIRCRAFT_URL.format(hex_code.lower()), timeout=5)
        if r.status_code == 200:
            _meta_reg = (r.json().get("registration") or "").strip().upper()
            if _meta_reg:
                _cache_db_set_reg(hex_code, _meta_reg)
        elif r.status_code == 429:
            _set_backoff("opensky_meta", secs=BACKOFF_RATE_LIMIT_SECS)
        elif r.status_code in (401, 403):
            _set_backoff("opensky_meta", secs=BACKOFF_AUTH_SECS)
    except Exception:
        pass


def get_aircraft_type(hex_code):
    """
    Aircraft type lookup priority:
      1. airplanes.live /v2/hex/{hex}  (best coverage, has desc field)
      2. adsbdb /v0/aircraft/{hex}     (static DB, manufacturer + type)
      3. OpenSky metadata /api/metadata/aircraft/icao/{hex}  (public, no token)
      4. airplanes.live /v2/reg/{reg}  (by tail # when hex lookup misses)
      5. FlightRadar24  get_flights(registration=reg)  (free last resort)
    Cache: type_string and source_label stored in the SQLite cache table.
    Returns (type_string, source_label).

    Registration side-effect: whenever a type is found but no reg is cached,
    _try_opensky_reg() is called to fill in the tail # from OpenSky metadata.
    The result is cached permanently so it is only fetched once per hex code.
    """
    if not hex_code:
        return "", "none"

    _cached_ac = _cache_db_get_aircraft(hex_code)
    if _cached_ac is not None:
        type_str, ac_source = _cached_ac
        if type_str:
            # Type is cached but reg may not be — fill it in opportunistically.
            if not _cache_db_get_reg(hex_code):
                _try_opensky_reg(hex_code)
            return type_str, f"{ac_source}:cached"   # known type, still fresh
        return "", "miss:cached"                       # recent miss — don't retry all 3 APIs yet

    # 1. airplanes.live
    try:
        r = requests.get(AIRPLANESLIVE_URL.format(hex_code.upper()), timeout=5)
        if r.status_code == 200:
            ac_list = r.json().get("ac", [])
            if ac_list:
                ac    = ac_list[0]
                plane = ac.get("desc", "") or ac.get("t", "") or ""
                reg   = (ac.get("r") or "").strip().upper()
                if reg:
                    _cache_db_set_reg(hex_code, reg)   # permanent; hex → tail never changes
                else:
                    _try_opensky_reg(hex_code)         # airplanes.live has no 'r' — fall back to OpenSky metadata
                if plane:
                    _cache_db_set_aircraft(hex_code, plane, "airplanes.live", AIRCRAFT_CACHE_TTL)
                    return plane, "airplanes.live"
    except Exception:
        pass

    # 2. adsbdb
    try:
        r = requests.get(ADSBDB_AIRCRAFT_URL.format(hex_code.lower()), timeout=5)
        if r.status_code == 200:
            ac = r.json().get("response", {}).get("aircraft", {})
            manufacturer = ac.get("manufacturer", "") or ""
            type_name = ac.get("type", "") or ""
            plane = _translate_type(f"{manufacturer} {type_name}".strip())
            if plane:
                _cache_db_set_aircraft(hex_code, plane, "adsbdb", AIRCRAFT_CACHE_TTL)
                return plane, "adsbdb"
    except Exception:
        pass

    # 3. OpenSky aircraft metadata (public endpoint — no token required)
    # This endpoint returns both aircraft type AND registration, so we extract
    # the registration here as a free fallback even when the type lookup misses.
    if not _in_backoff("opensky_meta"):
        try:
            r = requests.get(OPENSKY_AIRCRAFT_URL.format(hex_code.lower()), timeout=5)
            if r.status_code == 200:
                data = r.json()
                # Cache registration regardless of whether type is found — it's
                # a permanent hex→tail mapping that benefits future sightings.
                _meta_reg = (data.get("registration") or "").strip().upper()
                if _meta_reg:
                    _cache_db_set_reg(hex_code, _meta_reg)
                plane = _translate_type((data.get("model") or data.get("typecode") or "").strip())
                if plane:
                    _cache_db_set_aircraft(hex_code, plane, "opensky:meta", AIRCRAFT_CACHE_TTL)
                    return plane, "opensky:meta"
            elif r.status_code == 429:
                _set_backoff("opensky_meta", secs=BACKOFF_RATE_LIMIT_SECS)
            elif r.status_code in (401, 403):
                _set_backoff("opensky_meta", secs=BACKOFF_AUTH_SECS)
        except Exception:
            pass

    # 4. airplanes.live /v2/reg/{reg} — fallback when hex-based lookup missed but
    #    we know the registration (e.g. from fr24feed or the reg cache).
    #    Queries by tail # instead of live hex, so it works even when the aircraft
    #    isn't currently in airplanes.live's ADS-B feed.
    reg = _cache_db_get_reg(hex_code)
    if reg:
        try:
            r = requests.get(f"https://api.airplanes.live/v2/reg/{reg}", timeout=5)
            if r.status_code == 200:
                ac_list = r.json().get("ac", [])
                if ac_list:
                    ac    = ac_list[0]
                    plane = _translate_type(ac.get("desc", "") or ac.get("t", "") or "")
                    if plane:
                        _cache_db_set_aircraft(hex_code, plane, "airplanes.live:reg", AIRCRAFT_CACHE_TTL)
                        return plane, "airplanes.live:reg"
        except Exception:
            pass

    # 5. FlightRadar24 — free, no key required.  Queries by registration so only
    #    runs when a tail number is known (reg populated by step 4 above).
    #    Good last-resort for very new aircraft or foreign regs that static DBs miss.
    if reg and _FR24_AVAILABLE and not os.path.exists(FR24_DISABLED_FLAG):
        try:
            _fr24_api = _get_fr24_api()
            if _fr24_api is not None:
                with _fr24_lock:   # serialize concurrent get_flights() calls
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        _fr24_type_flights = _fr24_api.get_flights(registration=reg) or []
                _fr24_type_f = next(iter(_fr24_type_flights), None)
                if _fr24_type_f:
                    _fr24_ac_code = getattr(_fr24_type_f, 'aircraft_code', '') or ''
                    if _fr24_ac_code:
                        _fr24_ac_type = _translate_type(_fr24_ac_code)
                        _cache_db_set_aircraft(hex_code, _fr24_ac_type, "fr24", AIRCRAFT_CACHE_TTL)
                        return _fr24_ac_type, "fr24"
        except Exception:
            pass

    # Cache the miss so all 5 APIs aren't retried on every poll cycle.
    # AIRCRAFT_MISS_TTL (5 min) keeps retries reasonable without hammering the
    # APIs for aircraft that genuinely have no type data yet (e.g. new deliveries).
    _cache_db_set_aircraft(hex_code, "", "miss", AIRCRAFT_MISS_TTL)
    return "", "miss"


# ── Test flight lookup (no-cache, web-triggered) ──────────────────────────────

def run_test_lookup(callsign, use_cache=True):
    """
    Full test lookup for the web UI.

    use_cache=True  (default)
        Calls the real get_route() / get_aircraft_type() functions — the exact
        same code path as a live overhead flight, cache reads and all.  If the
        API waterfall order ever changes, this mode follows automatically because
        it literally IS those functions.

    use_cache=False
        Bypasses all cache reads; every API is called fresh.  Override rules
        are still applied (step 0).
        AirLabs and AeroAPI quota counters ARE incremented — these are real calls.

    Both modes respect backoffs and the _apis_disabled kill-switch.
    Writes to TEST_DISPLAY_FILE so the next grab_data() cycle injects the
    flight into the LED matrix for 30 seconds.
    Returns a comprehensive dict with per-step results for the web UI.
    """
    cs   = callsign.strip().upper()
    tag  = f"[TEST:{cs}]"
    mode = "cached" if use_cache else "no-cache"
    DISPLAY_SECS = 30
    now  = int(time.time())

    _log(f"{tag} ━━━ test lookup starting [{mode}] ━━━")

    result = {
        "callsign":          cs,
        "tail":              "",
        "use_cache":         use_cache,
        "hex_code":          "",
        "airborne":          False,
        "lat":               None,
        "lon":               None,
        "altitude":          None,
        "vertical_speed":    0,
        "override_matched":  False,
        "override":          None,
        "steps":             {},
        "final_origin":      "",
        "final_destination": "",
        "final_plane":       "",
        "route_source":      "none",
        "type_source":       "none",
        "display_injected":  False,
        "display_seconds":   DISPLAY_SECS,
    }

    # ── Live position via airplanes.live (common to both modes) ───────────────
    # Always run first — provides hex code and current position for plausibility
    # checks, plus an opportunistic type from the live feed.
    hex_code          = ""
    plane_lat         = None
    plane_lon         = None
    vertical_speed    = 0
    altitude_ft       = 10000
    _live_type_cached = ""   # type from the feed, used in no-cache type resolution

    for _kind, _url in [
        ("callsign", f"https://api.airplanes.live/v2/callsign/{cs}"),
        ("reg",      f"https://api.airplanes.live/v2/reg/{cs}"),
    ]:
        try:
            r = requests.get(_url, timeout=8)
            if r.status_code == 200:
                ac_list = r.json().get("ac", [])
                if ac_list:
                    ac             = ac_list[0]
                    hex_code       = (ac.get("hex") or "").lower()
                    plane_lat      = ac.get("lat")
                    plane_lon      = ac.get("lon")
                    vertical_speed = ac.get("baro_rate") or ac.get("geom_rate") or 0
                    _alt_raw    = ac.get("alt_baro") or ac.get("alt_geom") or 10000
                    altitude_ft = _alt_raw if isinstance(_alt_raw, (int, float)) else 10000
                    _live_type_cached = (ac.get("desc") or ac.get("t") or "").strip()
                    result.update({
                        "hex_code":       hex_code,
                        "tail":           (ac.get("r") or "").strip().upper(),
                        "airborne":       True,
                        "lat":            plane_lat,
                        "lon":            plane_lon,
                        "altitude":       int(altitude_ft),
                        "vertical_speed": int(vertical_speed),
                    })
                    result["steps"]["live_position"] = {
                        "found_by": _kind,
                        "hex":      hex_code,
                        "lat":      plane_lat,
                        "lon":      plane_lon,
                        "alt_ft":   int(altitude_ft),
                        "vs":       int(vertical_speed),
                        "type":     _live_type_cached,
                    }
                    _log(
                        f"{tag} [airplanes.live:{_kind}] hex={hex_code}"
                        f" alt={altitude_ft} vs={vertical_speed}"
                        f" pos=({plane_lat},{plane_lon})"
                    )
                    break
        except Exception as _e:
            _log(f"{tag} [airplanes.live:{_kind}] error: {_e}")

    if not result["airborne"]:
        _log(f"{tag} [airplanes.live] not currently airborne — static data only")
        result["steps"]["live_position"] = {"found_by": None, "airborne": False}

    # ── Branch: cached vs no-cache ────────────────────────────────────────────

    if use_cache:
        # ── CACHED MODE ───────────────────────────────────────────────────────
        # Calls the real production functions verbatim.  If the API waterfall in
        # get_route() or get_aircraft_type() ever changes, this test automatically
        # follows — no duplication, no divergence.
        result["steps"]["mode"] = "cache"

        origin, destination, route_src, override_plane, override_display = get_route(
            hex_code, cs, vertical_speed, plane_lat, plane_lon,
            registration=result.get("tail", ""),
        )
        plane, type_src = get_aircraft_type(hex_code)

        # Mirror _grab_data(): override_plane replaces stored type; override_display is display-only
        if override_plane:
            plane    = override_plane
            type_src = "override"

        plane        = plane if plane.upper() not in BLANK_FIELDS else ""
        display_name = override_display or plane   # after BLANK_FIELDS strip so sentinels don't leak

        # Capture override details if the override fired (route_src == "override")
        if route_src == "override":
            _ov = _match_override(cs)
            result["override_matched"] = True
            result["override"]         = dict(_ov) if _ov else {}

        result.update({
            "final_origin":      origin,
            "final_destination": destination,
            "final_plane":       plane,
            "final_display":     display_name,
            "route_source":      route_src,
            "type_source":       type_src,
        })
        result["steps"].update({
            "route_result": {"source": route_src,  "origin": origin, "destination": destination},
            "type_result":  {"source": type_src,   "plane":  plane},
        })
        _log(f"{tag} [route:{route_src}] {origin or '?'}->{destination or '?'}")
        _log(f"{tag} [type:{type_src}] '{plane}'")

    else:
        # ── NO-CACHE MODE ──────────────────────────────────────────────────────
        # Runs the EXACT production path — get_route() + get_aircraft_type() — with
        # cache READS bypassed (a thread-local flag), so every API is hit fresh.  No
        # reimplementation: the test flight follows real-flight logic by construction.
        # get_route() fills _trace with each source's raw result for the step grid.
        result["steps"]["mode"] = "no_cache"
        _trace = {}
        # Resolve the tail from the permanent hex→reg cache BEFORE bypassing reads.
        # Production's get_route() does this internally so FR24 §5 can run for a
        # commercial flight whose ADS-B feed omits the tail — but that internal read
        # is suppressed under the bypass.  Resolving it here keeps the no-cache test
        # faithful to a real flight; the hex→reg map is permanent identity data, not a
        # volatile route result, so reading it doesn't violate "call every API fresh".
        _test_reg = result.get("tail", "") or _cache_db_get_reg(hex_code)
        _cache_bypass.on = True
        try:
            origin, destination, route_src, override_plane, override_display = get_route(
                hex_code, cs, vertical_speed, plane_lat, plane_lon,
                registration=_test_reg, _trace=_trace,
            )
            plane, type_src = get_aircraft_type(hex_code)
        finally:
            _cache_bypass.on = False

        if override_plane:
            plane    = override_plane
            type_src = "override"
        plane        = plane if plane.upper() not in BLANK_FIELDS else ""
        display_name = override_display or plane

        if route_src == "override":
            _ov = _match_override(cs)
            result["override_matched"] = True
            result["override"]         = dict(_ov) if _ov else {}
            for _sk in ("adsbdb_route", "opensky", "airlabs", "aeroapi"):
                result["steps"][_sk] = {"skipped": "override"}
        else:
            result["steps"].update(_trace)   # per-source grid: adsbdb_route/opensky/airlabs/aeroapi

        result.update({
            "final_origin":      origin,
            "final_destination": destination,
            "final_plane":       plane,
            "final_display":     display_name,
            "route_source":      route_src,
            "type_source":       type_src,
        })
        result["steps"].update({
            "route_result": {"source": route_src, "origin": origin, "destination": destination},
            "type_result":  {"source": type_src,  "plane":  plane},
        })
        _log(f"{tag} [route:{route_src}] {origin or '?'}->{destination or '?'} (no-cache)")
        _log(f"{tag} [type:{type_src}] '{plane}' (no-cache)")
        # ── end no-cache mode ─────────────────────────────────────────────────

    # ── Summary ───────────────────────────────────────────────────────────────
    _log(
        f"{tag} ━━━ result:"
        f" {_route_display(result['final_origin'], result['final_destination'])}"
        f" [{result['route_source']}]"
        f" plane='{result['final_plane']}' [{result['type_source']}] ━━━"
    )

    # ── Display injection — 30 s window for the LED matrix ───────────────────
    _final_plane   = result["final_plane"]
    _final_display = result.get("final_display", "") or _final_plane
    _display_data = {
        "callsign":       cs,
        "plane":          _final_plane,
        "display_name":   _final_display,
        "origin":         result["final_origin"],
        "destination":    result["final_destination"],
        "altitude":       int(altitude_ft),
        "vertical_speed": int(vertical_speed),
        "test":           True,
        "expires":        now + DISPLAY_SECS,
    }
    try:
        _tmp = TEST_DISPLAY_FILE + ".tmp"
        with open(_tmp, "w") as _f:
            json.dump(_display_data, _f)
        os.replace(_tmp, TEST_DISPLAY_FILE)
        result["display_injected"] = True
        result["display_expires"]  = now + DISPLAY_SECS  # epoch so JS can compute true remaining
        _log(f"{tag} injected into display for {DISPLAY_SECS}s")
    except Exception as _e:
        _log(f"{tag} WARNING: failed to write test display file: {_e}")

    return result


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
        try:
            Thread(target=self._grab_data, daemon=True).start()
        except Exception:
            # If the thread can't start (rare resource exhaustion), don't leave
            # _processing stuck True forever — that would wedge all future polls.
            with self._lock:
                self._processing = False
            raise

    def _grab_data(self):
        with self._lock:
            self._new_data = False

        _purge_expired_cache()   # time-gated (~6 h); reclaims expired cache rows off the SD card

        data = []
        success = False
        try:
            all_flights = fetch_flights()
            in_zone_flights = [
                f for f in all_flights
                if MIN_ALTITUDE < f.altitude <= MAX_ALTITUDE and in_zone(f)
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
            # Read TEST_DISPLAY_FILE once here — reused for both callsign
            # exclusion (stats) and test-flight injection in the finally block.
            # A single read prevents a race where the file expires or changes
            # between the two uses.
            _td_cached = None
            try:
                _td_parsed = json.loads(_pathlib.Path(TEST_DISPLAY_FILE).read_text())
                if _td_parsed.get("expires", 0) > int(time.time()):
                    _td_cached = _td_parsed
            except Exception:
                pass
            _test_cs = (_td_cached.get("callsign") or "").strip().upper() if _td_cached else ""

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
                    flight.vrs_origin, flight.vrs_dest,
                    flight.registration,
                )
                _type_fut = _lookup_executor.submit(get_aircraft_type, flight.hex_code)
                origin, destination, route_src, override_plane, override_display = _route_fut.result()
                plane, type_src = _type_fut.result()

                # override_plane: replaces the stored aircraft type (stats + DB).
                # override_display: shown on the flight display only — real type still logged.
                # Legacy: if only override_plane is set (no display), it acts as both.
                if override_plane:
                    plane    = override_plane
                    type_src = "override"

                plane    = plane    if plane.upper()    not in BLANK_FIELDS else ""
                callsign = flight.callsign if flight.callsign.upper() not in BLANK_FIELDS else ""

                # What to show on the flight display marquee.
                # override_display wins if set; otherwise fall back to the real type.
                display_name = override_display or plane

                reg        = flight.registration or _cache_db_get_reg(flight.hex_code)
                # GA/helicopters often broadcast their N-number as the callsign with
                # no separate registration field — recognise and store it.
                if not reg and _N_NUMBER_RE.match(callsign):
                    reg = callsign.upper()
                    _cache_db_set_reg(flight.hex_code, reg)
                reg_suffix     = f" {reg}" if reg else ""
                display_suffix = f" [display:{display_name}]" if display_name and display_name != plane else ""
                # Log the route line only on first sighting or a change — not every poll,
                # so a lingering flight (e.g. a circling medevac helicopter) doesn't repeat
                # an identical line every ~15 s.  Tracking lines ([overhead] alt=) still print.
                _route_sig = (route_src, origin, destination, type_src, plane, display_name, reg)
                if _last_route_log.get(callsign) != _route_sig:
                    _bounded_put(_last_route_log, callsign, _route_sig)
                    _log(f"[route:{_display_src(route_src)}] [type:{_display_src(type_src)}]{display_suffix} {_airline_display(callsign)} {_route_display(origin, destination)} '{plane}'{reg_suffix}")
                if callsign != _test_cs:
                    _record_flight_stat(callsign, plane, origin, destination, reg, route_src)

                prev_was_live = _is_live(route_src) or _is_live(type_src)

                data.append({
                    "plane":        plane,         # real aircraft type — used for stats
                    "display_name": display_name,  # shown on flight display (may differ from plane)
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
                # ── Test flight injection ─────────────────────────────────────
                # If a test was triggered via the web UI, prepend the test
                # flight to data so it appears on the LED matrix and in
                # ft_data.json for the remaining duration of its 30 s window.
                # Uses _td_cached (read once above) to avoid a second read that
                # could race with the file expiring or being overwritten.
                try:
                    if _td_cached:
                        _td = _td_cached
                        _td_plane = _td.get("plane", "")
                        data.insert(0, {
                            "plane":          _td_plane,
                            "display_name":   _td.get("display_name", "") or _td_plane,
                            "origin":         _td.get("origin", ""),
                            "destination":    _td.get("destination", ""),
                            "vertical_speed": _td.get("vertical_speed", 0),
                            "altitude":       _td.get("altitude", 10000),
                            "callsign":       _td.get("callsign", ""),
                            "test":           True,
                        })
                except (KeyError, AttributeError, TypeError):
                    pass  # malformed file — skip injection silently
                # ─────────────────────────────────────────────────────────────

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

                # Cache writes happen immediately in each SQLite cache helper —
                # no periodic flush needed.

                with self._lock:
                    self._data = data

            _rotate_log_if_needed()

            with self._lock:
                if success:
                    self._new_data = True  # only signal new data when the poll actually succeeded
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
