# ── FlightTracker configuration ───────────────────────────────────────────────
# Copy this file to config.py and fill in your values.
# config.py is in .gitignore and will never be committed.

# ── Timezone ──────────────────────────────────────────────────────────────────
# IANA timezone name used for log timestamps and the web UI.
# Examples: "America/Los_Angeles", "America/New_York", "Europe/London", "Europe/Berlin"
# Full list: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones
TIMEZONE = "America/Los_Angeles"

# ── Zone — the bounding box of sky you want to monitor ────────────────────────
# Use a tool like https://boundingbox.klokantech.com to find your coordinates.
ZONE_HOME = {
    "tl_y": 0.000000,  # Top-Left Latitude  (deg)
    "tl_x": 0.000000,  # Top-Left Longitude (deg)
    "br_y": 0.000000,  # Bottom-Right Latitude  (deg)
    "br_x": 0.000000,  # Bottom-Right Longitude (deg)
}

# ── Home location — used to sort flights by distance ──────────────────────────
LOCATION_HOME = [
    0.000000,  # Latitude  (deg)
    0.000000,  # Longitude (deg)
    0.000,     # Altitude  (km above sea level)
]

# ── Weather ───────────────────────────────────────────────────────────────────
# Format: "city,state,country" or just "city" — spaces as %20
# Example: "new%20york,ny,us" or "london,uk"
WEATHER_LOCATION    = "your%20city,state,us"
OPENWEATHER_API_KEY = ""  # Free tier at https://openweathermap.org/price
TEMPERATURE_UNITS   = "imperial"  # "imperial" (°F) or "metric" (°C)

# ── Display ───────────────────────────────────────────────────────────────────
MIN_ALTITUDE = 100    # feet — filters out aircraft on the ground
MAX_ALTITUDE = 15000  # feet — set lower to filter out high-altitude overflights
BRIGHTNESS   = 80     # 0–100
GPIO_SLOWDOWN = 2     # 0–4 — increase if the display flickers (try 2 for Pi Zero)
HAT_PWM_ENABLED = True  # True if you've added the solder bridge to your HAT

# Highlight your nearest airport in bold on the display
JOURNEY_CODE_SELECTED = "XXX"  # 3-letter IATA code of your nearest airport
JOURNEY_BLANK_FILLER  = " ? "  # Shown when origin/destination is unknown

# ── ADS-B Receiver ────────────────────────────────────────────────────────────
# IP address of the machine running your ADS-B receiver software.
# Use "localhost" if it's on the same Pi, or an IP for a remote machine.
# For VRS on a non-standard port, include it here: "192.168.1.50:8090"
RECEIVER_HOST = "localhost"

# Receiver software type.
#   "dump1090" (default) — polls fr24feed (:8754) first, dump1090 (:8080) as fallback.
#   "vrs"                — polls Virtual Radar Server AircraftList.json API (:8080).
#                          VRS can ingest from dump1090, fr24feed, ADSB.im, and more.
#                          See: https://www.virtualradarserver.co.uk
RECEIVER_TYPE = "dump1090"

# ── Local airports ────────────────────────────────────────────────────────────
# Comma-separated IATA codes of airports in your area.
# Routes are only trusted from free APIs (adsbdb, OpenSky) when the flight
# is departing one of these airports — prevents accepting stale/wrong data.
LOCAL_AIRPORTS = "XXX,YYY"

# ── OpenSky Network — free, used for route history and aircraft registration ──
# Register at https://opensky-network.org
OPENSKY_CLIENT_ID     = ""
OPENSKY_CLIENT_SECRET = ""

# ── AirLabs — freemium route data, 1,000 calls/month per key ─────────────────
# Register at https://airlabs.co
AIRLABS_API_KEY   = ""  # Primary key
AIRLABS_API_KEY_2 = ""  # Optional secondary key — doubles your free monthly quota

# Billing period settings for the secondary key (primary uses the same reset day)
AIRLABS2_MONTHLY_LIMIT = 1000  # Calls per month on key 2
AIRLABS2_RESET_DAY     = 1     # Day of month your billing period resets (1–28)

# ── FlightAware AeroAPI — paid last resort ────────────────────────────────────
# Register at https://aeroapi.flightaware.com
# Only called when all free/freemium APIs return no route.
FLIGHTAWARE_API_KEY = ""
