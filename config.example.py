# ── FlightTracker configuration ───────────────────────────────────────────────
# Copy this file to config.py and fill in your values.
# config.py is in .gitignore and will never be committed.

# ── Timezone ──────────────────────────────────────────────────────────────────
# IANA timezone name used for log timestamps, the web UI, and the LED display.
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
MIN_ALTITUDE  = 100    # feet — filters out aircraft on the ground
MAX_ALTITUDE  = 15000  # feet — set lower to filter out high-altitude overflights
BRIGHTNESS    = 80     # 0–100, normal operating brightness
NIGHT_BRIGHTNESS = 20  # 0–100, applied when night mode is active (toggled in web UI)
GPIO_SLOWDOWN = 2      # 0–4 — increase if the display flickers (try 2 for Pi Zero)
HAT_PWM_ENABLED = True # True if you've added the solder bridge to your HAT

# Highlight your nearest airport in bold on the display
JOURNEY_CODE_SELECTED = "XXX"  # 3-letter IATA code of your nearest airport
JOURNEY_BLANK_FILLER  = " ? "  # Shown when origin/destination is unknown

# Date format — applies to the LED display and the web UI
DATE_FORMAT = "MDY"   # "MDY" = 5/14/2026 (US)  |  "DMY" = 14/5/2026 (UK/EU)  |  "YMD" = 2026-05-14 (ISO)

# ── Optional loading LED ──────────────────────────────────────────────────────
# An external LED on a GPIO pin that pulses while the display is loading.
# Leave LOADING_LED_ENABLED = False if you haven't wired one up.
LOADING_LED_ENABLED  = False
LOADING_LED_GPIO_PIN = 25   # BCM pin number

# ── Rainfall display (experimental) ──────────────────────────────────────────
# Shows a rainfall graph on the display. Requires a local taps-aff weather
# service — not the OpenWeather API. Leave False unless you've set that up.
RAINFALL_ENABLED = False

# ── Scoreboard display ────────────────────────────────────────────────────────
# When any configured team has a game today the idle display is replaced with
# a live scoreboard. Flights always take priority. When your team scores, a
# full-screen celebration scrolls for SCOREBOARD_GOAL_CELEBRATION_SECONDS.
# If multiple sports are live simultaneously, the first in SCOREBOARD_PRIORITY
# wins the display.
#
# Set SCOREBOARD_ENABLED = True to activate. Disabled by default — if you don't
# follow North American sports leagues there's no reason to turn this on.
#
# APIs used (all free, no keys required):
#   NHL — official NHL Stats API  https://api-web.nhle.com/v1/scoreboard/now
#   MLB — official MLB Stats API  https://statsapi.mlb.com/api/v1/teams?sportId=1
#   NFL / NBA / MLS — ESPN unofficial API (team IDs below)

SCOREBOARD_ENABLED = False   # master switch — set True to activate

# How long to keep showing the final score after the game ends (minutes)
SCOREBOARD_POST_GAME_MINUTES = 30

# Duration of the score celebration animation (seconds; flights suppressed)
SCOREBOARD_GOAL_CELEBRATION_SECONDS = 30

# Duration of the WIN celebration ("{team} WINS!") shown when a game goes final (seconds)
SCOREBOARD_WIN_CELEBRATION_SECONDS = 180

# Priority order — when multiple sports are live, the first entry wins
SCOREBOARD_PRIORITY = ["NHL", "NFL", "MLB", "NBA", "WNBA", "MLS", "FIFA"]

# ── NHL ───────────────────────────────────────────────────────────────────────
# Team IDs: use the link below and look for the numeric "id" field next to your team.
# https://api-web.nhle.com/v1/standings/now
# Example: 54 = Vegas Golden Knights, 10 = Toronto Maple Leafs, 6 = Boston Bruins
SCOREBOARD_NHL_ENABLED   = True
SCOREBOARD_NHL_TEAM_ID   = 0        # replace with your team's numeric ID
SCOREBOARD_NHL_TEAM_NAME = "NHL"    # ≤4 chars shown on LED display

# ── NFL ───────────────────────────────────────────────────────────────────────
# ESPN team IDs: https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams
SCOREBOARD_NFL_ENABLED   = False
SCOREBOARD_NFL_TEAM_ID   = 0        # replace with your team's numeric ID
SCOREBOARD_NFL_TEAM_NAME = "NFL"    # ≤4 chars shown on LED display

# ── MLB ───────────────────────────────────────────────────────────────────────
# MLB team IDs: https://statsapi.mlb.com/api/v1/teams?sportId=1
SCOREBOARD_MLB_ENABLED   = False
SCOREBOARD_MLB_TEAM_ID   = 0        # set your team's ID here
SCOREBOARD_MLB_TEAM_NAME = ""

# ── NBA ───────────────────────────────────────────────────────────────────────
# ESPN team IDs: https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams
# Note: NBA celebration is disabled — scores happen too frequently.
SCOREBOARD_NBA_ENABLED   = False
SCOREBOARD_NBA_TEAM_ID   = 0        # set your team's ID here
SCOREBOARD_NBA_TEAM_NAME = ""

# ── WNBA ──────────────────────────────────────────────────────────────────────
# ESPN team IDs: https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/teams
# Example: 17 = Las Vegas Aces, 5 = Indiana Fever, 9 = New York Liberty
# Note: like the NBA, celebration is disabled — scores happen too frequently.
SCOREBOARD_WNBA_ENABLED   = False
SCOREBOARD_WNBA_TEAM_ID   = 0        # set your team's ID here
SCOREBOARD_WNBA_TEAM_NAME = ""

# ── MLS ───────────────────────────────────────────────────────────────────────
# ESPN team IDs: https://site.api.espn.com/apis/site/v2/sports/soccer/usa.1/teams
SCOREBOARD_MLS_ENABLED   = False
SCOREBOARD_MLS_TEAM_ID   = 0        # set your team's ID here
SCOREBOARD_MLS_TEAM_NAME = ""

# ── FIFA World Cup (national teams) ────────────────────────────────────────────
# ESPN team IDs: https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/teams
# Example: 660 = United States, 202 = England, 205 = Brazil, 478 = Mexico
SCOREBOARD_FIFA_ENABLED   = False
SCOREBOARD_FIFA_TEAM_ID   = 0        # your national team's ESPN ID (e.g. 660 = USA)
SCOREBOARD_FIFA_TEAM_NAME = ""       # ≤4 chars shown on LED display (e.g. "USA")

# ── Remote agent apps (optional desktop companions) ──────────────────────────
# Two optional Windows desktop apps mirror the board over fire-and-forget UDP and
# play a sound — a hockey goal horn and a plane-overhead "ding". See remote-agents/.
# Leave the *_HOST values empty to disable (each sender becomes a complete no-op).
# Use the desktop machine's IP (not a hostname) so the Pi never blocks on a DNS lookup.
#
# Goal horn → remote-agents/goal-horn/  (fires on your team's goal / win)
SCOREBOARD_GOAL_HORN_HOST      = ""     # e.g. "192.168.1.30"; "" = off
SCOREBOARD_GOAL_HORN_PORT      = 50505
SCOREBOARD_GOAL_HORN_PING_SECS = 5      # heartbeat cadence (seconds)
#
# Plane ding → remote-agents/plane-ding/  (fires when a new aircraft goes on the matrix)
PLANE_DING_HOST      = ""               # e.g. "192.168.1.30"; "" = off
PLANE_DING_PORT      = 50506
PLANE_DING_PING_SECS = 5

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

# How often (in seconds) to poll the ADS-B receiver for new flights.
# Lower = faster to detect new aircraft entering your zone.
# 15 s is a good balance; go as low as 5 s if you want near-instant detection.
# Takes effect after Save & Restart.
POLL_INTERVAL = 15

# How often (in seconds) the display loop checks whether a background API
# lookup has finished and new flight data is ready to show.
# 2 s is fine for most setups; 1 s gives slightly faster transitions.
# Takes effect after Save & Restart.
DATA_CHECK_INTERVAL = 2

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

# Billing period for primary key
AIRLABS_MONTHLY_LIMIT = 1000  # Calls per month on key 1
AIRLABS_RESET_DAY     = 1     # Day of month your billing period resets (1–28)

# Billing period for secondary key
AIRLABS2_MONTHLY_LIMIT = 1000  # Calls per month on key 2
AIRLABS2_RESET_DAY     = 1     # Day of month your billing period resets (1–28)

# ── FlightAware AeroAPI — paid last resort ────────────────────────────────────
# Register at https://aeroapi.flightaware.com
# Only called when all free/freemium APIs return no route.
FLIGHTAWARE_API_KEY = ""

# Monthly credit from FlightAware:
#   $5.00  — standard free tier
#   $10.00 — if you run a FlightAware ADS-B feeder (fr24feed users often qualify)
#   Check your account at https://aeroapi.flightaware.com
FEEDER_MONTHLY_CREDIT = 5.00

# Day of month your AeroAPI billing period resets (1–28)
AEROAPI_RESET_DAY = 1

# ── Advanced: cache TTL overrides ────────────────────────────────────────────
# These have sensible defaults — only set them if you want to tune caching
# behaviour. Values are in seconds.
#
# ADSBDB_CACHE_TTL     = 3600    # adsbdb route result cache (default 1 hour)
# OPENSKY_CACHE_TTL    = 3600    # OpenSky route result cache (default 1 hour)
# ROUTE_TTL_SCHEDULED  = 604800  # Resolved commercial route cache (default 7 days)
# ROUTE_TTL_DEFAULT    = 3600    # Resolved GA/other route cache (default 1 hour)
# ROUTE_MISS_TTL       = 300     # No-route-found suppression (default 5 min)
# ROUTE_PAID_MISS_TTL  = 7200    # Paid API miss suppression (default 2 hours)

# ── Advanced: database retention ─────────────────────────────────────────────
# A low-frequency sweep bounds the two internal accuracy-stats tables so the SQLite DB
# can't grow forever on the SD card.  Your daily "sightings" history is kept indefinitely
# unless you opt into a window.
# API_CHECK_RETENTION_DAYS = 90   # prune internal accuracy stats older than N days
# SIGHTINGS_RETENTION_DAYS = 0    # 0 = keep all sighting history; set >0 (days) to prune
