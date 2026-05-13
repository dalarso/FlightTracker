import calendar
import fnmatch
import json
import math
import os
import re
import sqlite3
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
    "LXJ": "Flexjet",             "JRE": "flyExclusive",
    "TWY": "Solarius Aviation",
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
    "JNT",
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
    if _cache_conn is None:
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
    except Exception:
        pass


def _cache_db_get_aircraft(hex_code: str):
    """Return (type_str, source) for a fresh aircraft cache entry, or None."""
    if _cache_conn is None:
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
    if _cache_conn is None:
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
    if _cache_conn is None:
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
    Runs under _stats_lock using _db_conn — same serialisation as sightings.
    Never raises.
    """
    if _db_conn is None:
        return
    try:
        _now    = datetime.now(_PACIFIC)
        _seen   = _now.strftime("%Y-%m-%d %H:%M:%S")
        _date   = _now.strftime("%Y-%m-%d")
        with _stats_lock:
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

def _in_backoff(api_name: str) -> bool:
    """True if we should skip this API because it recently told us to back off."""
    until = _api_backoff.get(api_name, 0.0)
    return time.time() < until

def _set_backoff(api_name: str, secs: int = 3600) -> None:
    """Record a backoff period after receiving a rate-limit or auth error."""
    _api_backoff[api_name] = time.time() + secs
    _log(f"[{api_name}] backing off for {secs // 60} min")

def _check_period_reset(api_name: str, reset_day: int) -> None:
    """If this API is in backoff due to a 402 and a new billing period has since
    started, clear the backoff so the API resumes immediately rather than waiting
    up to 24 h for the old backoff timer to expire."""
    if api_name not in _api_credit_exhausted or not _in_backoff(api_name):
        return
    current_period = _billing_period_start(reset_day)
    if _api_credit_exhausted[api_name] != current_period:
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
        self.hex_code = hex_code
        self.registration = registration.strip().upper() if registration else ""

    @classmethod
    def from_fr24(cls, hex_code, entry):
        try:
            lat = entry[FR24_LAT]
            lon = entry[FR24_LON]
            if not lat or not lon:
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
            registration=ac.get("registration", "") or "",
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
    today = datetime.now(_PACIFIC)
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


def get_route(hex_code, callsign, vertical_speed, plane_lat=None, plane_lon=None):
    """
    Route lookup priority:
      0.  Override rules — ft_overrides.json; pattern-matched against callsign.
                           Returns immediately, no API calls made at all.
      0.5 Resolved-route cache (scheduled airlines only) — written at the end of
                           any successful full-route resolution; checked before
                           any API work so repeat daily flights skip the chain.
      1.  adsbdb       — static historical DB; trusted only when the ORIGIN is a
                         local airport (see LOCAL_AIRPORTS in config.py) AND geometry plausibility passes.
      2.  OpenSky      — free, unlimited, real-time by ICAO24 hex.  Trusted only when
                         the ORIGIN is a local airport.  If origin=local, dest is also
                         accepted from the same result; a missing dest falls through to
                         AirLabs.  Arrival-only and through-traffic are skipped.
      3.  AirLabs      — real-time by callsign; geometry plausibility check applied.
                         Trusted only when origin is a local airport.
                         1,000 calls/month free.
      4.  FlightAware  — paid last resort; geometry plausibility + origin-local check.
                         Cascades on 402/429.
      If no source supplies a validated departure route, origin/dest → "?".

    Trust rule: only routes WHERE THE PLANE IS DEPARTING a local airport are accepted.
    ~90%+ of in-zone overhead traffic is departing a configured local airport;
    arrivals and through-traffic show "?" rather than risk accepting stale/wrong data.

    Cache format: (origin, dest, orig_lat, orig_lon, dest_lat, dest_lon, timestamp)
    Negative cache entries have empty origin/dest with None coords — used to
    avoid re-querying APIs within ROUTE_MISS_TTL when they had no data.

    plane_lat/plane_lon: aircraft's current position, used for plausibility checks.
    vertical_speed: kept for API compatibility; no longer used in trust logic.
    """
    origin, destination, source = "", "", ""
    now = int(time.time())
    _apis_disabled = os.path.exists(APIS_DISABLED_FLAG)  # evaluate once — two callers below

    # Override display/type fields — populated if any override rule matches;
    # returned at the end so partial overrides (missing endpoints) still carry
    # display name and aircraft type through the free-API fill-in path.
    _ov_plane   = ""
    _ov_display = ""
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
        _override_partial = True
        _log(f"[override] {callsign}: partial override — polling free APIs to fill missing endpoint(s)")

    # ── 0.5. Resolved-route cache (scheduled airlines only) ───────────────────
    # When we successfully resolve both endpoints for a scheduled airline
    # callsign (from any combination of sources), the final result is cached
    # here at the scheduled-airline TTL (7 days).  A hit here skips the entire
    # API chain — no adsbdb, OpenSky, AirLabs, or AeroAPI calls are made.
    #
    # This prevents AirLabs call burns on repeat sightings of daily flights
    # where OpenSky sometimes returns a full route (so AirLabs is never called
    # and its 7-day cache is never written) and sometimes returns only partial
    # data (triggering a live AirLabs call to fill the missing endpoint).
    if callsign and _route_ttl(callsign) == ROUTE_TTL_SCHEDULED:
        _resolved = _cache_db_get_route(callsign, 'resolved')
        if _resolved and _resolved[0] and _resolved[1]:
            _rsrc = _resolved[6].removesuffix(":cached")  # strip any trailing :cached so it isn't doubled
            _resolved_label = f"resolved:{_rsrc}:cached" if _rsrc else "resolved:cached"
            return _resolved[0], _resolved[1], _resolved_label, "", ""  # 4th: override plane  5th: override display

    # ── 1. adsbdb (static historical DB) ──────────────────────────────────────
    adsbdb_origin = adsbdb_dest = ""
    adsbdb_olat = adsbdb_olon = adsbdb_dlat = adsbdb_dlon = None
    _adsbdb_src = "adsbdb"

    if callsign and not os.path.exists(ADSBDB_DISABLED_FLAG):
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
    _adsbdb_commercial   = (bool(callsign) and len(callsign) >= 3
                             and callsign[:3].upper() in _SCHEDULED_PREFIXES)
    _adsbdb_origin_local = adsbdb_origin.upper() in _LOCAL_AIRPORTS if adsbdb_origin else False
    adsbdb_ok = (
        not _adsbdb_commercial           # commercial flights require paid-API confirmation
        and _adsbdb_origin_local
        and _route_plausible(plane_lat, plane_lon,
                             adsbdb_olat, adsbdb_olon,
                             adsbdb_dlat, adsbdb_dlon)
    )
    if adsbdb_ok:
        origin, destination, source = adsbdb_origin, adsbdb_dest, _adsbdb_src
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

    # ── 2. OpenSky by hex (free, unlimited — queried before AirLabs) ────────────
    # OpenSky doesn't return airport coordinates, so geometry plausibility isn't
    # possible.  Trusted only when the ORIGIN is a local airport (departing local).
    # If trusted, the destination is also accepted from the same result; a missing
    # dest falls through to AirLabs.  Arrival-only and through-traffic are skipped.
    #
    _sky_origin = _sky_dest = ""
    _sky_src = "opensky"
    # OpenSky is free/unlimited — intentionally excluded from the _apis_disabled kill-switch.
    if not (origin and destination) and OPENSKY_CLIENT_ID and hex_code and not _in_backoff("opensky") and not os.path.exists(OPENSKY_DISABLED_FLAG):
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
                        params={"icao24": hex_code.lower(), "begin": now - 21600, "end": now},
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=10,
                    )
                    if r.status_code == 429:
                        _set_backoff("opensky", secs=3600)
                    elif r.status_code in (401, 403):
                        _set_backoff("opensky", secs=86400)
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
                        _sky_ttl = OPENSKY_CACHE_TTL if (_sky_origin or _sky_dest) else ROUTE_MISS_TTL
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
    # adsbdb keys by callsign; OpenSky keys by hex code — two independent sources.
    # If both return the exact same non-local route AND it passes the geometry
    # plausibility check (using adsbdb's airport coordinates), trust it without
    # burning a paid API call.  Both origin AND destination must agree; partial
    # matches fall through to AirLabs as before.
    if not (origin and destination) and not _adsbdb_commercial:
        if (adsbdb_origin and _sky_origin
                and adsbdb_origin == _sky_origin
                and adsbdb_dest and _sky_dest
                and adsbdb_dest == _sky_dest):
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
        _cached_al = _cache_db_get_route(f"airlabs:{callsign}", 'route')
        _check_period_reset("airlabs", AIRLABS_RESET_DAY)
        if _cached_al:
            al_origin, al_dest = _cached_al[0], _cached_al[1]
            al_olat, al_olon = _cached_al[2], _cached_al[3]
            al_dlat, al_dlon = _cached_al[4], _cached_al[5]
            _al_src = "airlabs:cached"
            # No log — suppress cached-hit spam; live fetches produce a log below
        elif not _in_backoff("airlabs"):
            try:
                r = requests.get(
                    AIRLABS_URL,
                    params={"flight_icao": callsign, "api_key": AIRLABS_API_KEY},
                    timeout=5,
                )
                if r.status_code == 429:
                    _set_backoff("airlabs", secs=3600)
                elif r.status_code == 402:
                    _set_backoff("airlabs", secs=86400)
                    _api_credit_exhausted["airlabs"] = _billing_period_start(AIRLABS_RESET_DAY)
                    _log("[airlabs-1] ⚠ 402 — monthly call limit exceeded; disabling for 24 h")
                elif r.status_code in (401, 403):
                    _set_backoff("airlabs", secs=86400)
                    _log(f"[airlabs-1] auth error ({r.status_code}) — check AIRLABS_API_KEY")
                elif r.status_code == 200:
                    _al_count = _airlabs_increment()
                    resp = r.json().get("response") or {}
                    al_origin = resp.get("dep_iata", "") or ""
                    al_dest   = resp.get("arr_iata", "") or ""
                    al_olat   = resp.get("dep_lat")
                    al_olon   = resp.get("dep_lng")
                    al_dlat   = resp.get("arr_lat")
                    al_dlon   = resp.get("arr_lng")
                    if not (al_origin or al_dest):
                        _log(f"[airlabs-1] {callsign}: no data [call #{_al_count}]")
                    # Cache result (even empty — negative cache suppresses retries)
                    _al_ttl = _route_ttl(callsign) if (al_origin or al_dest) else ROUTE_MISS_TTL
                    _cache_db_set_route(f"airlabs:{callsign}", 'route',
                                        al_origin, al_dest,
                                        al_olat, al_olon, al_dlat, al_dlon,
                                        int(time.time()) + _al_ttl,
                                        source="airlabs")
                else:
                    # Unexpected status (e.g. 404, 500) — count the call (AirLabs may
                    # bill any HTTP request), negatively cache to prevent per-poll retries.
                    _al_count = _airlabs_increment()
                    _log(f"[airlabs-1] {callsign}: unexpected status {r.status_code} [call #{_al_count}] — negative caching")
                    _cache_db_set_route(f"airlabs:{callsign}", 'route',
                                        "", "", None, None, None, None,
                                        int(time.time()) + ROUTE_MISS_TTL,
                                        source="airlabs")
            except Exception as e:
                _log(f"[airlabs-1] {callsign}: request error — {e}")
        else:
            _log(f"[airlabs-1] {callsign}: in backoff — skipping")

    # AirLabs returns coordinates — apply geometry plausibility only.
    # Paid APIs trust any plausible route (arrivals and through-traffic included);
    # origin-local restriction applies only to free APIs (adsbdb, OpenSky).
    if al_origin or al_dest:
        al_plausible = _route_plausible(plane_lat, plane_lon,
                                         al_olat, al_olon, al_dlat, al_dlon)
        if al_plausible:
            _al_filled_dest = not destination
            if source and _al_filled_dest:
                # A free API already set the origin; AirLabs is filling the missing
                # destination.  But if AirLabs' own origin disagrees with what the
                # free API found, the two sources describe different routes — mixing
                # them would produce a nonsensical "FreeSrc_origin→AirLabs_dest" route.
                # In that case, prefer AirLabs' complete authoritative route instead.
                if al_origin and origin and al_origin.upper() != origin.upper():
                    _log(f"[airlabs-1] origin conflict ({origin} vs {al_origin}) — preferring AirLabs-1 complete route")
                    origin = al_origin
                    destination = al_dest
                    source = _al_src
                else:
                    # Origins agree (or AirLabs returned no origin) — fill dest only
                    # and credit both sources in the tag.
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
        else:
            if _al_src != "airlabs:cached":
                _log(f"[airlabs-1] implausible route {al_origin}->{al_dest} rejected for {callsign}")

    # ── 3b. AirLabs 2 (secondary key — fallback when AirLabs 1 is in backoff) ──
    # Only tried when AirLabs 1 didn't make a live 200 call this invocation
    # (_al_count == 0 covers: in-backoff, disabled, not configured, or cache-hit).
    # Both keys call the same AirLabs backend, so a live AirLabs 1 empty response
    # means AirLabs 2 would return the same — skip it and preserve quota.
    al2_origin = al2_dest = ""
    al2_olat = al2_olon = al2_dlat = al2_dlon = None
    _al2_src   = "airlabs2"
    _al2_count = 0

    if (not (origin and destination) and _al_count == 0
            and AIRLABS_API_KEY_2 and callsign
            and not _apis_disabled and not os.path.exists(AIRLABS2_DISABLED_FLAG)
            and not _skip_paid):
        _cached_al2 = _cache_db_get_route(f"airlabs2:{callsign}", 'route')
        _check_period_reset("airlabs2", AIRLABS2_RESET_DAY)
        if _cached_al2:
            al2_origin, al2_dest = _cached_al2[0], _cached_al2[1]
            al2_olat, al2_olon = _cached_al2[2], _cached_al2[3]
            al2_dlat, al2_dlon = _cached_al2[4], _cached_al2[5]
            _al2_src = "airlabs2:cached"
        elif not _in_backoff("airlabs2"):
            try:
                r = requests.get(
                    AIRLABS_URL,
                    params={"flight_icao": callsign, "api_key": AIRLABS_API_KEY_2},
                    timeout=5,
                )
                if r.status_code == 429:
                    _set_backoff("airlabs2", secs=3600)
                elif r.status_code == 402:
                    _set_backoff("airlabs2", secs=86400)
                    _api_credit_exhausted["airlabs2"] = _billing_period_start(AIRLABS2_RESET_DAY)
                    _log("[airlabs-2] ⚠ 402 — monthly call limit exceeded; disabling for 24 h")
                elif r.status_code in (401, 403):
                    _set_backoff("airlabs2", secs=86400)
                    _log(f"[airlabs-2] auth error ({r.status_code}) — check AIRLABS_API_KEY_2")
                elif r.status_code == 200:
                    _al2_count = _airlabs2_increment()
                    resp = r.json().get("response") or {}
                    al2_origin = resp.get("dep_iata", "") or ""
                    al2_dest   = resp.get("arr_iata", "") or ""
                    al2_olat   = resp.get("dep_lat")
                    al2_olon   = resp.get("dep_lng")
                    al2_dlat   = resp.get("arr_lat")
                    al2_dlon   = resp.get("arr_lng")
                    if not (al2_origin or al2_dest):
                        _log(f"[airlabs-2] {callsign}: no data [call #{_al2_count}]")
                    _al2_ttl = _route_ttl(callsign) if (al2_origin or al2_dest) else ROUTE_MISS_TTL
                    _cache_db_set_route(f"airlabs2:{callsign}", 'route',
                                        al2_origin, al2_dest,
                                        al2_olat, al2_olon, al2_dlat, al2_dlon,
                                        int(time.time()) + _al2_ttl,
                                        source="airlabs2")
                else:
                    _al2_count = _airlabs2_increment()
                    _log(f"[airlabs-2] {callsign}: unexpected status {r.status_code} [call #{_al2_count}] — negative caching")
                    _cache_db_set_route(f"airlabs2:{callsign}", 'route',
                                        "", "", None, None, None, None,
                                        int(time.time()) + ROUTE_MISS_TTL,
                                        source="airlabs2")
            except Exception as e:
                _log(f"[airlabs-2] {callsign}: request error — {e}")
        else:
            _log(f"[airlabs-2] {callsign}: in backoff — skipping")

    if al2_origin or al2_dest:
        al2_plausible = _route_plausible(plane_lat, plane_lon,
                                          al2_olat, al2_olon, al2_dlat, al2_dlon)
        if al2_plausible:
            _al2_filled_dest = not destination
            if source and _al2_filled_dest:
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
        else:
            if _al2_src != "airlabs2:cached":
                _log(f"[airlabs-2] implausible route {al2_origin}->{al2_dest} rejected for {callsign}")

    # ── 4. FlightAware AeroAPI (paid — last resort, capped at monthly limit) ───
    if not (origin and destination) and FLIGHTAWARE_API_KEY and callsign and not _apis_disabled and not os.path.exists(AEROAPI_DISABLED_FLAG) and not _skip_paid:
        _cached_fa = _cache_db_get_route(callsign, 'aeroapi')
        if _cached_fa:
            # Apply geometry plausibility even on cache hits — a 7-day-old AeroAPI
            # result for the same callsign could be from a different prior flight leg.
            _cached_plausible = not _cached_fa[0] or _route_plausible(
                plane_lat, plane_lon, _cached_fa[2], _cached_fa[3],
                _cached_fa[4], _cached_fa[5]
            )
            if _cached_plausible:
                if not origin:
                    origin = _cached_fa[0]
                if not destination:
                    destination = _cached_fa[1]
                source = source or "aeroapi:cached"
            # No log — suppress cached-hit spam; live fetches produce a log below
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
                        _fa_orig  = f.get("origin") or {}
                        _fa_dest  = f.get("destination") or {}
                        fa_origin = _fa_orig.get("code_iata", "") or ""
                        fa_dest   = _fa_dest.get("code_iata", "") or ""
                        fa_olat   = _fa_orig.get("latitude")
                        fa_olon   = _fa_orig.get("longitude")
                        fa_dlat   = _fa_dest.get("latitude")
                        fa_dlon   = _fa_dest.get("longitude")
                    _aeroapi_increment()  # logs running spend
                    if not (fa_origin or fa_dest):
                        _log(f"[aeroapi] {callsign}: no flights returned")
                    _fa_ttl = _route_ttl(callsign) if (fa_origin or fa_dest) else ROUTE_MISS_TTL
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
                            _fa_filled_dest = not destination
                            if source and _fa_filled_dest:
                                # If AeroAPI's origin conflicts with the one already set by a
                                # prior source, prefer AeroAPI's complete authoritative route
                                # rather than mixing a free-API origin with AeroAPI's destination.
                                if fa_origin and origin and fa_origin.upper() != origin.upper():
                                    _log(f"[aeroapi] origin conflict ({origin} vs {fa_origin}) — preferring AeroAPI complete route")
                                    origin = fa_origin
                                    destination = fa_dest
                                    source = "aeroapi"
                                else:
                                    if not origin:
                                        origin = fa_origin
                                    destination = fa_dest
                                    source = f"{source}+aeroapi"
                            else:
                                if not origin:
                                    origin = fa_origin
                                if not destination:
                                    destination = fa_dest
                                source = source or "aeroapi"
                            _log(f"[aeroapi] {_airline_display(callsign)}: {_route_display(fa_origin, fa_dest)} accepted")
                        else:
                            _log(f"[aeroapi] {callsign}: {fa_origin}->{fa_dest} rejected — implausible route")
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
    if (not origin and not destination and callsign
            and (_airlabs_was_eligible or _airlabs2_was_eligible)
            and _aeroapi_was_eligible):
        _cache_db_set_paid_miss(callsign)
        _log(f"[route] {callsign}: all paid APIs returned empty — suppressing for {ROUTE_PAID_MISS_TTL // 3600}h")

    # Cross-check log — for commercial flights where adsbdb had data, report whether
    # the paid APIs confirmed or overrode it (GA flights never enter this branch).
    if _adsbdb_commercial and (adsbdb_origin or adsbdb_dest) and (origin or destination):
        _db_route   = f"{adsbdb_origin or '?'}->{adsbdb_dest or '?'}"
        _paid_route = f"{origin or '?'}->{destination or '?'}"
        _matched    = ((adsbdb_origin or "").upper() == (origin or "").upper()
                       and (adsbdb_dest or "").upper() == (destination or "").upper())
        if _matched:
            _log(f"[adsbdb] {callsign}: paid APIs confirmed adsbdb route ({_paid_route})")
        else:
            _log(f"[adsbdb] {callsign}: paid APIs overrode adsbdb — was {_db_route}, now {_paid_route}")
        _record_free_api_check(callsign, _db_route, _paid_route, _matched)

    # ── Resolved-route cache write ─────────────────────────────────────────────
    # If both endpoints are now known for a scheduled airline, cache the final
    # result so future sightings of the same daily flight skip the full API
    # chain — even when individual upstream caches (e.g. OpenSky's 1-hour TTL)
    # have expired or only have partial data.
    if (origin and destination and callsign
            and _route_ttl(callsign) == ROUTE_TTL_SCHEDULED):
        _cache_db_set_route(callsign, 'resolved',
                            origin, destination,
                            None, None, None, None,
                            int(time.time()) + ROUTE_TTL_SCHEDULED,
                            source=source.removesuffix(":cached"))

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
            _set_backoff("opensky_meta", secs=3600)
        elif r.status_code in (401, 403):
            _set_backoff("opensky_meta", secs=86400)
    except Exception:
        pass


def get_aircraft_type(hex_code):
    """
    Aircraft type lookup priority:
      1. airplanes.live /v2/hex/{hex}  (best coverage, has desc field)
      2. adsbdb /v0/aircraft/{hex}     (static DB, manufacturer + type)
      3. OpenSky metadata /api/metadata/aircraft/icao/{hex}  (public, no token)
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
                _set_backoff("opensky_meta", secs=3600)
            elif r.status_code in (401, 403):
                _set_backoff("opensky_meta", secs=86400)
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

    # Cache the miss so all 4 APIs aren't retried on every poll cycle.
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
            hex_code, cs, vertical_speed, plane_lat, plane_lon
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
        # ── NO-CACHE MODE ─────────────────────────────────────────────────────
        # Every API called fresh; cache is never read.  Override rules still
        # apply first (step 0).
        result["steps"]["mode"] = "no_cache"

        # ── 0. Override rules ──────────────────────────────────────────────────
        _ov = _match_override(cs)
        if _ov:
            ov_origin   = (_ov.get("origin")   or "").strip().upper()
            ov_dest     = (_ov.get("destination") or "").strip().upper()
            ov_plane    = (_ov.get("plane")    or "").strip()
            ov_display  = (_ov.get("display")  or "").strip()
            _log(
                f"{tag} [override] matched '{_ov['pattern']}'"
                f" → {ov_origin or '?'}->{ov_dest or '?'}"
                + (f"  display='{ov_display}'" if ov_display else "")
                + (f"  type='{ov_plane}'" if ov_plane else "")
                + (f"  note: {_ov['note']}" if _ov.get("note") else "")
            )
            result["override_matched"]  = True
            result["override"]          = dict(_ov)
            result["final_origin"]      = ov_origin
            result["final_destination"] = ov_dest
            result["final_plane"]       = ov_plane
            result["final_display"]     = ov_display
            result["route_source"]      = "override"
            if ov_plane:
                result["type_source"]   = "override"
            for _sk in ("adsbdb_route", "opensky", "airlabs", "aeroapi"):
                result["steps"][_sk] = {"skipped": "override"}

        _route_resolved = bool(result["final_origin"] or result["final_destination"])
        _apis_disabled  = os.path.exists(APIS_DISABLED_FLAG)  # evaluate once for both AirLabs + AeroAPI

        if not _route_resolved:

            # ── 1. adsbdb route (fresh) ────────────────────────────────────────
            _adsbdb_origin = _adsbdb_dest = ""
            _adsbdb_olat = _adsbdb_olon = _adsbdb_dlat = _adsbdb_dlon = None
            _sky_origin = _sky_dest = ""   # initialised here; set in step 2 if OpenSky runs
            try:
                r = requests.get(ADSBDB_CALLSIGN_URL.format(cs), timeout=5)
                result["steps"]["adsbdb_route"] = {"status": r.status_code}
                if r.status_code == 200:
                    fr = (r.json().get("response") or {}).get("flightroute") or {}
                    _adsbdb_origin = (fr.get("origin") or {}).get("iata_code", "") or ""
                    _adsbdb_dest   = (fr.get("destination") or {}).get("iata_code", "") or ""
                    _adsbdb_olat   = (fr.get("origin") or {}).get("latitude")
                    _adsbdb_olon   = (fr.get("origin") or {}).get("longitude")
                    _adsbdb_dlat   = (fr.get("destination") or {}).get("latitude")
                    _adsbdb_dlon   = (fr.get("destination") or {}).get("longitude")
                    result["steps"]["adsbdb_route"].update({
                        "origin": _adsbdb_origin, "destination": _adsbdb_dest,
                    })
                    _log(f"{tag} [adsbdb] route: {_adsbdb_origin or '?'}->{_adsbdb_dest or '?'}")
                elif r.status_code == 404:
                    _log(f"{tag} [adsbdb] callsign not in DB (404)")
                else:
                    _log(f"{tag} [adsbdb] unexpected status {r.status_code}")
            except Exception as _e:
                _log(f"{tag} [adsbdb] error: {_e}")
                result["steps"]["adsbdb_route"] = {"error": str(_e)}

            if _adsbdb_origin or _adsbdb_dest:
                _adsbdb_origin_local = _adsbdb_origin.upper() in _LOCAL_AIRPORTS if _adsbdb_origin else False
                _adsbdb_ok = (
                    _adsbdb_origin_local
                    and _route_plausible(plane_lat, plane_lon,
                                         _adsbdb_olat, _adsbdb_olon,
                                         _adsbdb_dlat, _adsbdb_dlon)
                )
                result["steps"]["adsbdb_route"]["origin_local"] = _adsbdb_origin_local
                if _adsbdb_ok:
                    result["final_origin"]      = _adsbdb_origin
                    result["final_destination"] = _adsbdb_dest
                    result["route_source"]      = "adsbdb"
                    _route_resolved             = True
                    _log(f"{tag} [adsbdb] accepted — local origin")
                elif not _adsbdb_origin_local:
                    _log(f"{tag} [adsbdb] skipped — origin not local ({_adsbdb_origin or '?'})")
                else:
                    _log(f"{tag} [adsbdb] skipped — route implausible")

            # ── 2. OpenSky (fresh, respects backoff) ───────────────────────────
            if not (result["final_origin"] and result["final_destination"]) and OPENSKY_CLIENT_ID and hex_code:
                if _in_backoff("opensky"):
                    _log(f"{tag} [opensky] in backoff — skipping")
                    result["steps"]["opensky"] = {"skipped": "backoff"}
                else:
                    _token = _get_opensky_token()
                    if not _token:
                        _log(f"{tag} [opensky] no token available")
                        result["steps"]["opensky"] = {"skipped": "no_token"}
                    else:
                        try:
                            r = requests.get(
                                OPENSKY_FLIGHTS_URL,
                                params={"icao24": hex_code.lower(), "begin": now - 21600, "end": now},
                                headers={"Authorization": f"Bearer {_token}"},
                                timeout=10,
                            )
                            result["steps"]["opensky"] = {"status": r.status_code}
                            if r.status_code == 429:
                                _set_backoff("opensky", secs=3600)
                                _log(f"{tag} [opensky] 429 — rate limited")
                            elif r.status_code in (401, 403):
                                _set_backoff("opensky", secs=86400)
                                _log(f"{tag} [opensky] auth error {r.status_code}")
                            elif r.status_code == 200:
                                _sky_data = r.json() or []
                                if _sky_data:
                                    _fl = max(_sky_data, key=lambda f: f.get("firstSeen", 0))
                                    _sky_origin = icao_to_iata(_fl.get("estDepartureAirport") or "")
                                    _sky_dest   = icao_to_iata(_fl.get("estArrivalAirport") or "")
                                    result["steps"]["opensky"].update({
                                        "origin": _sky_origin, "destination": _sky_dest,
                                    })
                                    _log(f"{tag} [opensky] route: {_sky_origin or '?'}->{_sky_dest or '?'}")
                                    if _sky_origin or _sky_dest:
                                        _sky_origin_local = _sky_origin.upper() in _LOCAL_AIRPORTS if _sky_origin else False
                                        result["steps"]["opensky"]["origin_local"] = _sky_origin_local
                                        if _sky_origin_local:
                                            result["final_origin"]      = result["final_origin"] or _sky_origin
                                            result["final_destination"] = result["final_destination"] or _sky_dest
                                            result["route_source"]      = "opensky"
                                            _route_resolved             = True
                                            _log(f"{tag} [opensky] accepted — local origin")
                                        else:
                                            _log(f"{tag} [opensky] skipped — origin not local ({_sky_origin or '?'})")
                                else:
                                    _log(f"{tag} [opensky] no flight history in last 6h")
                                    result["steps"]["opensky"]["no_data"] = True
                        except Exception as _e:
                            _log(f"{tag} [opensky] error: {_e}")
                            result["steps"]["opensky"] = {"error": str(_e)}

            # ── 2b. Free-API consensus (mirrors get_route logic) ───────────────
            if not (result["final_origin"] and result["final_destination"]):
                if (_adsbdb_origin and _sky_origin
                        and _adsbdb_origin == _sky_origin
                        and _adsbdb_dest and _sky_dest
                        and _adsbdb_dest == _sky_dest):
                    _con_ok = _route_plausible(plane_lat, plane_lon,
                                               _adsbdb_olat, _adsbdb_olon,
                                               _adsbdb_dlat, _adsbdb_dlon)
                    result["steps"]["consensus"] = {
                        "origin": _adsbdb_origin, "destination": _adsbdb_dest,
                        "plausible": _con_ok,
                    }
                    if _con_ok:
                        result["final_origin"]      = _adsbdb_origin
                        result["final_destination"] = _adsbdb_dest
                        result["route_source"]      = "adsbdb+opensky"
                        _route_resolved             = True
                        _log(f"{tag} [consensus] {_adsbdb_origin}->{_adsbdb_dest} — free APIs agree")
                    else:
                        _log(f"{tag} [consensus] {_adsbdb_origin}->{_adsbdb_dest} — agreed but implausible")

            # ── 3. AirLabs (fresh, counts quota, respects backoff + kill-switch) ─
            _test_skip_paid = _skip_paid_apis(cs)
            if _test_skip_paid:
                _log(f"{tag} [airlabs-1] skipping — N-number or military callsign")
            if not (result["final_origin"] and result["final_destination"]) and AIRLABS_API_KEY and cs and not _test_skip_paid:
                if _in_backoff("airlabs"):
                    _log(f"{tag} [airlabs-1] in backoff — skipping")
                    result["steps"]["airlabs"] = {"skipped": "backoff"}
                elif _apis_disabled or os.path.exists(AIRLABS_DISABLED_FLAG):
                    _log(f"{tag} [airlabs-1] disabled — skipping")
                    result["steps"]["airlabs"] = {"skipped": "disabled"}
                else:
                    try:
                        r = requests.get(
                            AIRLABS_URL,
                            params={"flight_icao": cs, "api_key": AIRLABS_API_KEY},
                            timeout=5,
                        )
                        result["steps"]["airlabs"] = {"status": r.status_code}
                        if r.status_code == 429:
                            _set_backoff("airlabs", secs=3600)
                            _log(f"{tag} [airlabs-1] 429 — rate limited")
                        elif r.status_code == 402:
                            _set_backoff("airlabs", secs=86400)
                            _api_credit_exhausted["airlabs"] = _billing_period_start(AIRLABS_RESET_DAY)
                            _log(f"{tag} [airlabs-1] ⚠ 402 — monthly call limit exceeded; disabling for 24 h")
                        elif r.status_code in (401, 403):
                            _set_backoff("airlabs", secs=86400)
                            _log(f"{tag} [airlabs-1] auth error {r.status_code}")
                        elif r.status_code == 200:
                            _al_count = _airlabs_increment()
                            _resp = r.json().get("response") or {}
                            _al_origin = _resp.get("dep_iata", "") or ""
                            _al_dest   = _resp.get("arr_iata", "") or ""
                            _al_olat   = _resp.get("dep_lat")
                            _al_olon   = _resp.get("dep_lng")
                            _al_dlat   = _resp.get("arr_lat")
                            _al_dlon   = _resp.get("arr_lng")
                            result["steps"]["airlabs"].update({
                                "origin": _al_origin, "destination": _al_dest,
                            })
                            _log(f"{tag} [airlabs-1] route: {_al_origin or '?'}->{_al_dest or '?'} [call #{_al_count}]")
                            if _al_origin or _al_dest:
                                _al_ok = _route_plausible(plane_lat, plane_lon,
                                                           _al_olat, _al_olon, _al_dlat, _al_dlon)
                                result["steps"]["airlabs"]["origin_local"] = (
                                    _al_origin.upper() in _LOCAL_AIRPORTS if _al_origin else False
                                )
                                result["steps"]["airlabs"]["plausible"] = _al_ok
                                # Paid APIs trust any plausible route — no origin-local restriction.
                                if _al_ok:
                                    result["final_origin"]      = result["final_origin"] or _al_origin
                                    result["final_destination"] = result["final_destination"] or _al_dest
                                    result["route_source"]      = "airlabs"
                                    _route_resolved             = True
                                else:
                                    _log(f"{tag} [airlabs-1] route implausible — rejected")
                        else:
                            _al_count = _airlabs_increment()
                            _log(f"{tag} [airlabs-1] unexpected status {r.status_code} [call #{_al_count}] — no data")
                    except Exception as _e:
                        _log(f"{tag} [airlabs-1] error: {_e}")
                        result["steps"]["airlabs"] = {"error": str(_e)}

            # ── 3b. AirLabs 2 (secondary key — same logic as production path) ─
            # Only tried when AirLabs 1 didn't make a live call (_al_count == 0).
            _al2_count = 0
            if (not (result["final_origin"] and result["final_destination"])
                    and _al_count == 0
                    and AIRLABS_API_KEY_2 and cs and not _test_skip_paid):
                if _in_backoff("airlabs2"):
                    _log(f"{tag} [airlabs-2] in backoff — skipping")
                    result["steps"]["airlabs2"] = {"skipped": "backoff"}
                elif _apis_disabled or os.path.exists(AIRLABS2_DISABLED_FLAG):
                    _log(f"{tag} [airlabs-2] disabled — skipping")
                    result["steps"]["airlabs2"] = {"skipped": "disabled"}
                else:
                    try:
                        r = requests.get(
                            AIRLABS_URL,
                            params={"flight_icao": cs, "api_key": AIRLABS_API_KEY_2},
                            timeout=5,
                        )
                        result["steps"]["airlabs2"] = {"status": r.status_code}
                        if r.status_code == 429:
                            _set_backoff("airlabs2", secs=3600)
                            _log(f"{tag} [airlabs-2] 429 — rate limited")
                        elif r.status_code == 402:
                            _set_backoff("airlabs2", secs=86400)
                            _api_credit_exhausted["airlabs2"] = _billing_period_start(AIRLABS2_RESET_DAY)
                            _log(f"{tag} [airlabs-2] ⚠ 402 — monthly call limit exceeded; disabling for 24 h")
                        elif r.status_code in (401, 403):
                            _set_backoff("airlabs2", secs=86400)
                            _log(f"{tag} [airlabs-2] auth error {r.status_code}")
                        elif r.status_code == 200:
                            _al2_count = _airlabs2_increment()
                            _resp2 = r.json().get("response") or {}
                            _al2_origin = _resp2.get("dep_iata", "") or ""
                            _al2_dest   = _resp2.get("arr_iata", "") or ""
                            _al2_olat   = _resp2.get("dep_lat")
                            _al2_olon   = _resp2.get("dep_lng")
                            _al2_dlat   = _resp2.get("arr_lat")
                            _al2_dlon   = _resp2.get("arr_lng")
                            result["steps"]["airlabs2"].update({
                                "origin": _al2_origin, "destination": _al2_dest,
                            })
                            _log(f"{tag} [airlabs-2] route: {_al2_origin or '?'}->{_al2_dest or '?'} [call #{_al2_count}]")
                            if _al2_origin or _al2_dest:
                                _al2_ok = _route_plausible(plane_lat, plane_lon,
                                                            _al2_olat, _al2_olon, _al2_dlat, _al2_dlon)
                                result["steps"]["airlabs2"]["plausible"] = _al2_ok
                                if _al2_ok:
                                    result["final_origin"]      = result["final_origin"] or _al2_origin
                                    result["final_destination"] = result["final_destination"] or _al2_dest
                                    result["route_source"]      = "airlabs2"
                                else:
                                    _log(f"{tag} [airlabs-2] route implausible — rejected")
                        else:
                            _al2_count = _airlabs2_increment()
                            _log(f"{tag} [airlabs-2] unexpected status {r.status_code} [call #{_al2_count}] — no data")
                    except Exception as _e:
                        _log(f"{tag} [airlabs-2] error: {_e}")
                        result["steps"]["airlabs2"] = {"error": str(_e)}

            # ── 4. AeroAPI (fresh, counts spend, respects backoff + kill-switch) ─
            if _test_skip_paid:
                result["steps"]["aeroapi"] = {"skipped": "n_number_or_military"}
            if not (result["final_origin"] and result["final_destination"]) and FLIGHTAWARE_API_KEY and cs and not _test_skip_paid:
                if _in_backoff("aeroapi"):
                    _log(f"{tag} [aeroapi] in backoff — skipping")
                    result["steps"]["aeroapi"] = {"skipped": "backoff"}
                elif _apis_disabled or os.path.exists(AEROAPI_DISABLED_FLAG):
                    _log(f"{tag} [aeroapi] disabled — skipping")
                    result["steps"]["aeroapi"] = {"skipped": "disabled"}
                else:
                    try:
                        r = requests.get(
                            AEROAPI_URL.format(cs),
                            headers={"x-apikey": FLIGHTAWARE_API_KEY},
                            timeout=10,
                        )
                        result["steps"]["aeroapi"] = {"status": r.status_code}
                        if r.status_code == 429:
                            _set_backoff("aeroapi", secs=3600)
                            _log(f"{tag} [aeroapi] 429 — rate limited")
                        elif r.status_code == 402:
                            _set_backoff("aeroapi", secs=86400)
                            _log(f"{tag} [aeroapi] 402 — credit exhausted")
                        elif r.status_code in (401, 403):
                            _set_backoff("aeroapi", secs=86400)
                            _log(f"{tag} [aeroapi] auth error {r.status_code}")
                        elif r.status_code == 200:
                            _aeroapi_increment()
                            _flights = r.json().get("flights", [])
                            _active  = [f for f in _flights if not f.get("actual_on")]
                            _fa_fl   = _active[0] if _active else (_flights[0] if _flights else None)
                            if _fa_fl:
                                _fa_origin = (_fa_fl.get("origin") or {}).get("code_iata", "") or ""
                                _fa_dest   = (_fa_fl.get("destination") or {}).get("code_iata", "") or ""
                                _fa_olat   = (_fa_fl.get("origin") or {}).get("latitude")
                                _fa_olon   = (_fa_fl.get("origin") or {}).get("longitude")
                                _fa_dlat   = (_fa_fl.get("destination") or {}).get("latitude")
                                _fa_dlon   = (_fa_fl.get("destination") or {}).get("longitude")
                                result["steps"]["aeroapi"].update({
                                    "origin": _fa_origin, "destination": _fa_dest,
                                })
                                _log(f"{tag} [aeroapi] route: {_fa_origin or '?'}->{_fa_dest or '?'}")
                                if _fa_origin or _fa_dest:
                                    _fa_ok = _route_plausible(plane_lat, plane_lon,
                                                               _fa_olat, _fa_olon, _fa_dlat, _fa_dlon)
                                    result["steps"]["aeroapi"]["origin_local"] = (
                                        _fa_origin.upper() in _LOCAL_AIRPORTS if _fa_origin else False
                                    )
                                    result["steps"]["aeroapi"]["plausible"] = _fa_ok
                                    # Paid APIs trust any plausible route — no origin-local restriction.
                                    if _fa_ok:
                                        result["final_origin"]      = result["final_origin"] or _fa_origin
                                        result["final_destination"] = result["final_destination"] or _fa_dest
                                        result["route_source"]      = "aeroapi"
                                        _route_resolved             = True
                                    else:
                                        _log(f"{tag} [aeroapi] route implausible — rejected")
                            else:
                                _log(f"{tag} [aeroapi] no active flight found")
                        else:
                            _log(f"{tag} [aeroapi] status {r.status_code} — no data")
                    except Exception as _e:
                        _log(f"{tag} [aeroapi] error: {_e}")
                        result["steps"]["aeroapi"] = {"error": str(_e)}

        # ── Aircraft type (no-cache): airplanes.live feed data, then adsbdb, then OpenSky meta ──
        if not result["final_plane"] and _live_type_cached:
            result["final_plane"] = _live_type_cached
            result["type_source"] = "airplanes.live"
            _log(f"{tag} [type:airplanes.live] '{_live_type_cached}'")

        if not result["final_plane"] and hex_code:
            try:
                r = requests.get(ADSBDB_AIRCRAFT_URL.format(hex_code), timeout=5)
                result["steps"]["adsbdb_type"] = {"status": r.status_code}
                if r.status_code == 200:
                    _ac    = r.json().get("response", {}).get("aircraft", {})
                    _mfr   = (_ac.get("manufacturer") or "").strip()
                    _tp    = (_ac.get("type") or "").strip()
                    _atype = f"{_mfr} {_tp}".strip()
                    result["steps"]["adsbdb_type"]["type"] = _atype
                    if _atype:
                        result["final_plane"] = _atype
                        result["type_source"] = "adsbdb"
                        _log(f"{tag} [type:adsbdb] '{_atype}'")
                    else:
                        _log(f"{tag} [type:adsbdb] no type data")
                else:
                    _log(f"{tag} [type:adsbdb] status {r.status_code}")
            except Exception as _e:
                _log(f"{tag} [type:adsbdb] error: {_e}")
                result["steps"]["adsbdb_type"] = {"error": str(_e)}

        if not result["final_plane"] and hex_code:
            try:
                r = requests.get(OPENSKY_AIRCRAFT_URL.format(hex_code.lower()), timeout=5)
                result["steps"]["opensky_meta"] = {"status": r.status_code}
                if r.status_code == 200:
                    _odata = r.json()
                    # Extract registration — permanent mapping, cache it even in
                    # test mode so future real flights benefit from this lookup.
                    _ometa_reg = (_odata.get("registration") or "").strip().upper()
                    if _ometa_reg:
                        _cache_db_set_reg(hex_code, _ometa_reg)
                        if not result["tail"]:
                            result["tail"] = _ometa_reg
                        result["steps"]["opensky_meta"]["registration"] = _ometa_reg
                    _otype = (_odata.get("model") or _odata.get("typecode") or "").strip()
                    result["steps"]["opensky_meta"]["type"] = _otype
                    if _otype:
                        result["final_plane"] = _otype
                        result["type_source"] = "opensky:meta"
                        _log(f"{tag} [type:opensky:meta] '{_otype}'")
                    else:
                        _log(f"{tag} [type:opensky:meta] no type data")
                else:
                    _log(f"{tag} [type:opensky:meta] status {r.status_code}")
            except Exception as _e:
                _log(f"{tag} [type:opensky:meta] error: {_e}")
                result["steps"]["opensky_meta"] = {"error": str(_e)}

        if not result["final_plane"]:
            result["type_source"] = "miss"
            _log(f"{tag} [type] no data from any source")
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
            # Determine if a test lookup is active — if so, its callsign must
            # not be counted in stats even if the same aircraft happens to be
            # physically in the zone at the same time.
            _test_cs = ""
            try:
                _td_check = json.loads(_pathlib.Path(TEST_DISPLAY_FILE).read_text())
                if _td_check.get("expires", 0) > int(time.time()):
                    _test_cs = (_td_check.get("callsign") or "").strip().upper()
            except Exception:
                pass

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
                _log(f"[route:{route_src}] [type:{type_src}]{display_suffix} {_airline_display(callsign)} {_route_display(origin, destination)} '{plane}'{reg_suffix}")
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
                try:
                    _td = json.loads(_pathlib.Path(TEST_DISPLAY_FILE).read_text())
                    if _td.get("expires", 0) > int(time.time()):
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
                except (FileNotFoundError, ValueError, KeyError,
                        AttributeError, TypeError):
                    pass  # malformed/missing file — skip injection silently
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
