# ✈ FlightTracker — RGB Matrix Flight Display with Intelligent Route Intelligence

> **⚠️ Disclaimer: I am not a developer.** This project was built entirely using [Claude Code](https://claude.ai/code) (Anthropic's AI coding assistant). It works well for my specific setup, but it has not been formally tested, audited, or hardened for general use. **Use at your own risk.** Pull requests and issue reports are welcome, but I may not be able to diagnose or fix problems beyond my own environment.

A heavily extended fork of [Colin Waddell's](https://blog.colinwaddell.com/flight-tracker/) RGB Matrix Flight Tracker. Colin built the original display hardware integration, the animation engine, the ADS-B receiver polling, and the core concept of showing overhead flights on an LED matrix. All of that remains the heart of this project.

**Original project:** [github.com/ColinWaddell/FlightTracker](https://github.com/ColinWaddell/FlightTracker)  
**Colin's blog post:** [blog.colinwaddell.com/flight-tracker](https://blog.colinwaddell.com/flight-tracker/)

> ☢️ Colin has also written about a company selling unauthorized copies of this hardware with a hidden backdoor. [Read his warning here.](https://colinwaddell.com/articles/flight-tracker-led-ripoff-part-2-its-so-much-worse)

---

## What this fork adds

This version takes the original display concept and builds a full flight intelligence stack around it — a multi-tier API routing engine, a SQLite-backed data layer, and a web-based management UI. The goal is to show not just *what* is overhead, but *where it came from and where it's going* — reliably, efficiently, and with minimal API spend.

### My specific use case

I live in Las Vegas directly under a departure corridor from LAS (Harry Reid International). Overhead flights are pulled from **my own ADS-B receiver** — an RTL-SDR dongle running `dump1090` — which picks up aircraft transponder signals directly. No third-party feed subscription required; you just need the hardware in range. The receiver host is configurable, so you can also point it at a network-accessible `dump1090` instance, `fr24feed`, Virtual Radar Server, or any compatible JSON source on another machine.

Roughly **90% of the traffic I see is a commercial departure out of LAS** — scheduled airline flights with filed flight plans. The routing logic reflects this: free historical databases are trusted conservatively (they can't know today's specific flight assignment), while real-time paid APIs are used as the authoritative source for commercial routes. If your local traffic skews toward GA aircraft or you're not near a major hub, you may find the free-API trust rules are more permissive than you need — or less. The `LOCAL_AIRPORTS` config and the override rules table give you the main levers to tune this.

---

## Hardware

- Raspberry Pi (any model with GPIO)
- 64×32 RGB LED Matrix panel
- Adafruit RGB Matrix Bonnet
- ADS-B receiver (RTL-SDR dongle + antenna) running `dump1090`, `fr24feed`, or Virtual Radar Server

---

## Architecture Overview

```
ADS-B Receiver (dump1090 / fr24feed / VRS)
        │
        ▼
  Zone + Altitude Filter
  (bounding box, MIN/MAX altitude)
        │
        ▼
  Sort by distance from home
  (up to 5 flights per poll)
        │
   ┌────┴────┐
   │         │  (parallel threads)
   ▼         ▼
get_route() get_aircraft_type()
   │         │
   └────┬────┘
        │
        ▼
  LED Matrix Display
  ft_data.json (shared state)
        │
        ▼
  Flask Web UI (port 5000)
  SQLite Database (ft_flights.db)
```

---

## Route Resolution — API Hierarchy

For each overhead flight, the route engine works through a prioritized stack, stopping as soon as a trusted result is found. Every result is cached in SQLite to avoid redundant calls.

### Step 0 — Override Rules
User-defined rules stored in SQLite. Pattern-match by callsign (wildcards supported). Overrides return immediately — no API calls are made. Example: `JANET*` → always display "Janet Airlines" departing LAS.

### Step 0.5 — Resolved-Route Cache *(scheduled airlines only)*
For commercial flights (recognized by ICAO 3-letter prefix), once both endpoints are known from any source, the full route is cached for 7 days — along with the airport coordinates. On subsequent sightings, the cached route is validated with a **geometry plausibility check** before being returned. If the aircraft's current position is inconsistent with the stored route (e.g. the same flight number is being reused for a different city-pair on a different day), the stale cache entry is busted and a fresh lookup runs. This prevents wrong routes from sticking for the full 7-day TTL when an airline reuses a flight number on a different routing.

### Step 0.75 — VRS Live Route Hint *(Virtual Radar Server only)*
When the receiver is Virtual Radar Server, it may supply departure and arrival airport codes directly in the feed. Trusted immediately if at least one endpoint is a configured local airport. Stored with a 1-hour TTL so it doesn't interfere with the 7-day resolved cache.

### Step 1 — adsbdb *(free, unlimited)*
Static historical database queried by callsign. Reliable for GA flights — a callsign like `N12345` consistently maps to the same aircraft. For commercial airlines, the result is cached and logged but **not committed** — the historical DB can't know today's specific routing. Cached for 1 hour.

### Step 2 — OpenSky Flights API *(free, unlimited)*
Queries the last 6 hours of flight history by hex code (aircraft radio ID). Returns estimated departure and arrival airports. Trusted only when the departure is a configured local airport. Like adsbdb, **not committed for commercial flights** — paid APIs are authoritative. Cached for 1 hour.

### Step 2b — Free-API Consensus
If adsbdb and OpenSky independently return the **exact same route** (using completely different lookup keys — callsign vs. hex) and geometry plausibility passes, the route is trusted without burning a paid API call. Only applies to GA flights.

### Step 3 — AirLabs Primary Key *(1,000 calls/month free)*
Real-time route lookup by callsign. Returns airport coordinates enabling geometry plausibility checks. Trusted for any plausible route regardless of origin — paid APIs are not restricted to local departures. Results cached at the scheduled-airline TTL (7 days) or GA TTL (1 hour). Automatically disables on 402 with 24-hour backoff; resumes immediately when the billing period resets.

### Step 3b — AirLabs Secondary Key *(1,000 calls/month free)*
A second AirLabs API key used as overflow. Fires only when the primary key did not make a live call this invocation (backoff, cache hit, disabled). If the primary returned an empty live response, the secondary is skipped — both keys hit the same backend. Combined, the two keys provide 2,000 free calls/month before AeroAPI spend begins.

### Step 4 — FlightAware AeroAPI *(paid, last resort)*
Paid real-time route lookup, capped and tracked monthly. Only called when all free/freemium APIs failed to resolve the route. Geometry plausibility is applied even on cache hits since the same callsign can fly a different route on a different day.

### Paid-Miss Cache
When both AirLabs and AeroAPI return empty for the same callsign, a 2-hour suppression entry is written. Prevents repeated quota burns on GA or obscure flights that will never have a filed route.

### Cache TTL Reference

| Cache type | TTL | Notes |
|---|---|---|
| Resolved commercial route | 7 days | Set by `ROUTE_TTL_SCHEDULED` — once origin+destination confirmed by any trusted source; geometry-validated on read |
| AirLabs / AeroAPI result | 7 days (commercial) / 1 hour (GA) | Same TTL as resolved route when committed |
| adsbdb result | 1 hour | Cached but not committed for commercial flights |
| OpenSky result | 1 hour | Cached but not committed for commercial flights |
| Paid-miss (no route found) | 2 hours | Suppresses redundant paid API calls for unresolvable callsigns |
| Aircraft type / registration | Indefinite | Hardware doesn't change — permanent mapping |
| AirLabs 402 backoff | Until billing period resets | Clears automatically on reset day; no restart needed |

TTLs can be overridden in `config.py` — see `ROUTE_TTL_SCHEDULED`, `ROUTE_TTL_DEFAULT`, and the other cache TTL constants.

### Cross-Check & Accuracy Tracking
For commercial flights where adsbdb had a route, the result is compared against what the paid APIs returned and stored in the `free_api_checks` table. The web UI shows a running match-rate percentage — useful for understanding how reliable the free historical DB is for your local traffic.

---

## Aircraft Type Resolution

Parallel to route lookup, a separate thread resolves the aircraft type for display and logging:

1. **airplanes.live** — returns type code and registration in one call
2. **adsbdb** type endpoint — fallback if airplanes.live misses
3. **OpenSky metadata** (`/api/aircraft/HEX`) — registration and model by hex; permanent mapping cached indefinitely
4. **airplanes.live /v2/reg** — registration-only fallback

Type codes (e.g. `B738`) are translated to human-readable names (e.g. `Boeing 737-800`) via a built-in lookup table covering airliners, regional jets, business jets, GA aircraft, and helicopters.

---

## Database — `ft_flights.db`

SQLite with WAL mode and NORMAL sync (SD-card friendly). Two persistent connections:

| Connection | Tables | Purpose |
|---|---|---|
| `_db_conn` | `sightings`, `api_calls`, `free_api_checks` | Flight stats and history |
| `_cache_conn` | `cache`, `overrides`, `overrides_meta` | Route/type cache and override rules |

### Key Tables

**`sightings`** — every unique flight seen overhead (deduped per callsign per day). Stores callsign, registration, origin, destination, aircraft type, route source, and timestamp.

**`cache`** — unified route and type cache. Supports multiple `cache_type` values (`route`, `aeroapi`, `resolved`, `paid_miss`, `aircraft`, `reg`) with per-entry TTL expiry. Resolved-route entries also store origin and destination airport coordinates for geometry plausibility checks on cache reads.

**`overrides`** — user-defined routing rules. Managed via the web UI, version-counter invalidated so edits take effect on the next poll without a restart.

**`api_calls`** — per-day API call counts by source (airlabs, aeroapi, adsbdb, opensky). Powers the daily sparklines in the web UI.

**`free_api_checks`** — per-flight record of whether adsbdb matched the paid API result for commercial flights. Powers the accuracy card on the stats page.

---

## Web UI — `server.py` (port 5000)

A Flask application running as a separate systemd service (`FlightTrackerWeb.service`). Provides:

### Live Display Tab
- Current overhead flights with route, aircraft type, and registration
- Leaflet map showing the configured zone
- Manual route test: enter any callsign to run the full resolution stack and see exactly which APIs returned what

### Stats Tab
- **Today**: flights seen, unique airlines, API calls used
- **Top Airlines**: sorted by sighting count, with N-number registrations (private / GA) grouped into a single "N-reg" row
- **Top Tail #s**: 25 most-seen registrations, each annotated with airline name and aircraft type where known
- **Recent Flights**: last 20 sightings with origin → destination and aircraft type
- **Free API Accuracy**: 30-day adsbdb vs. paid API match rate with mismatch history
- **90-Day Period**: rolling totals with per-day sparklines, filterable by custom date range

### API Management Tab
Stack of all configured APIs showing:
- Enable/disable toggles (flag files in `/tmp/` — auto-cleared on reboot)
- Monthly usage with progress bars and billing period dates
- Manual usage adjustment (correct the counter if a period was mis-counted)
- Per-API notes explaining trust rules and caching behavior

### Config Tab
Live editing of `config.py` without SSH:
- Location and zone settings
- Timezone (IANA name)
- API keys (masked by default)
- Billing limits and reset days for AirLabs and AeroAPI
- Cache TTL overrides
- Display options: brightness, night brightness, poll intervals, time/date format
- Save & Restart button
- Footer links to this repo and Colin Waddell's original project

### Overrides Tab
Full CRUD for the override rule table:
- Add/edit/delete/reorder rules
- Wildcard patterns (e.g. `JANET*`)
- Per-flight display name (shown on LED), aircraft type, origin, destination, note
- Changes take effect on the next poll via version-counter cache invalidation

### Cache Management
- Clear cache by source (adsbdb, opensky, airlabs-1, airlabs-2, aeroapi, resolved, paid-miss)
- Live cache statistics: entry counts by source

---

## Pause & Night Mode

Two flag-file controls let you manage the display without restarting the service:

| Flag file | Effect |
|---|---|
| `/tmp/ft_paused` | Blanks the matrix (brightness → 0) while the file exists |
| `/tmp/ft_night` | Drops brightness to `NIGHT_BRIGHTNESS` while the file exists |

The web UI's Display tab has toggle buttons for both. Flag files are in `/tmp/` so they auto-clear on reboot — the display always comes back at full brightness after a power cycle.

---

## Configuration — `config.py`

Copy `config.example.py` to `config.py` and fill in your values. `config.py` is gitignored and never committed.

```python
# ── Timezone ──────────────────────────────────────────────────────────────────
TIMEZONE = "America/Los_Angeles"   # IANA name — https://en.wikipedia.org/wiki/List_of_tz_database_time_zones

# ── Zone — bounding box of sky to monitor ─────────────────────────────────────
ZONE_HOME = {
    "tl_y": 0.000000,   # Top-Left Latitude
    "tl_x": 0.000000,   # Top-Left Longitude
    "br_y": 0.000000,   # Bottom-Right Latitude
    "br_x": 0.000000,   # Bottom-Right Longitude
}
LOCATION_HOME = [0.000000, 0.000000, 0.000]  # lat, lon, alt (km)

# ── ADS-B Receiver ────────────────────────────────────────────────────────────
RECEIVER_HOST = "localhost"       # IP of your dump1090 / fr24feed / VRS host
RECEIVER_TYPE = "dump1090"        # "dump1090" or "vrs" (Virtual Radar Server)
POLL_INTERVAL       = 15          # seconds between receiver polls (min 5)
DATA_CHECK_INTERVAL = 2           # seconds between display data-pickup checks (min 1)

# ── Local airports ────────────────────────────────────────────────────────────
LOCAL_AIRPORTS = "XXX,YYY"        # comma-separated IATA codes — controls route trust

# ── Display ───────────────────────────────────────────────────────────────────
MIN_ALTITUDE     = 100            # feet — filters out ground traffic
MAX_ALTITUDE     = 15000          # feet — filters out high-altitude overflights
BRIGHTNESS       = 80             # 0–100, normal operating brightness
NIGHT_BRIGHTNESS = 20             # 0–100, applied when /tmp/ft_night flag exists
GPIO_SLOWDOWN    = 2              # 0–4, increase if flickering
HAT_PWM_ENABLED  = True           # requires solder bridge on HAT
JOURNEY_CODE_SELECTED = "XXX"     # IATA code of your nearest airport
JOURNEY_BLANK_FILLER  = " ? "     # placeholder for unknown airports
TIME_FORMAT = "24h"               # "24h" (14:30) or "12h" (2:30 PM)
DATE_FORMAT = "MDY"               # "MDY", "DMY", or "YMD"

# ── Optional loading LED ──────────────────────────────────────────────────────
LOADING_LED_ENABLED  = False      # external LED that pulses while loading
LOADING_LED_GPIO_PIN = 25         # BCM pin number

# ── Weather ───────────────────────────────────────────────────────────────────
WEATHER_LOCATION    = "your%20city,state,us"  # spaces as %20
OPENWEATHER_API_KEY = ""          # free tier at openweathermap.org (optional)
TEMPERATURE_UNITS   = "imperial"  # "imperial" (°F) or "metric" (°C)
RAINFALL_ENABLED    = False       # experimental — requires local taps-aff service

# ── OpenSky Network — free route history and aircraft registration ─────────────
OPENSKY_CLIENT_ID     = ""        # opensky-network.org
OPENSKY_CLIENT_SECRET = ""

# ── AirLabs — freemium route data, 1,000 calls/month per key ──────────────────
AIRLABS_API_KEY        = ""       # primary key — airlabs.co
AIRLABS_API_KEY_2      = ""       # optional secondary key (separate quota)
AIRLABS_MONTHLY_LIMIT  = 1000
AIRLABS_RESET_DAY      = 1        # day of month billing resets (1–28)
AIRLABS2_MONTHLY_LIMIT = 1000
AIRLABS2_RESET_DAY     = 1

# ── FlightAware AeroAPI — paid last resort ────────────────────────────────────
FLIGHTAWARE_API_KEY    = ""       # aeroapi.flightaware.com
FEEDER_MONTHLY_CREDIT  = 5.00     # $10 if you run a FlightAware ADS-B feeder
AEROAPI_RESET_DAY      = 1

# ── Advanced: cache TTL overrides (seconds) ───────────────────────────────────
# ADSBDB_CACHE_TTL     = 3600     # adsbdb result cache (default 1 hour)
# OPENSKY_CACHE_TTL    = 3600     # OpenSky result cache (default 1 hour)
# ROUTE_TTL_SCHEDULED  = 604800   # Resolved commercial route (default 7 days)
# ROUTE_TTL_DEFAULT    = 3600     # Resolved GA/other route (default 1 hour)
# ROUTE_MISS_TTL       = 3600     # No-route suppression (default 1 hour)
# ROUTE_PAID_MISS_TTL  = 7200     # Paid API miss suppression (default 2 hours)
```

---

## Systemd Services

Two independent services — restart them separately when deploying changes:

```bash
# After changing overhead.py, flight-tracker.py, or display/__init__.py:
sudo systemctl restart FlightTracker.service

# After changing server.py:
sudo systemctl restart FlightTrackerWeb.service

# index.html takes effect immediately — no restart needed
```

Check logs (both write to `/home/pi/plane.log`):

```bash
tail -f /home/pi/plane.log
```

---

## Override Rules

Override rules let you hard-code routing and display info for specific aircraft, bypassing the entire API stack. Rules are managed in the web UI and stored in SQLite.

| Field | Purpose |
|---|---|
| `pattern` | Callsign pattern — exact match or wildcard (e.g. `JANET*`, `N145DV`) |
| `origin` | Force departure airport (IATA code) |
| `destination` | Force arrival airport (IATA code) |
| `display` | Custom label shown on the LED marquee |
| `plane` | Aircraft type stored in stats |
| `note` | Internal note (not displayed) |

---

## Trust Rules Summary

| Callsign type | adsbdb | OpenSky | AirLabs | AeroAPI |
|---|---|---|---|---|
| **Commercial** (SWA, DAL, etc.) | Cached + logged only | Logged only | ✅ Trusted | ✅ Trusted (last resort) |
| **GA** (N-numbers) | ✅ If local origin | ✅ If local origin | ⛔ Skipped | ⛔ Skipped |
| **Military/Gov** (RCH, SAM…) | ✅ If local origin | ✅ If local origin | ⛔ Skipped | ⛔ Skipped |
| **Override match** | ⛔ Skipped | ⛔ Skipped | ⛔ Skipped | ⛔ Skipped |

GA and military callsigns skip paid APIs because N-numbers don't file instrument flight plans that AirLabs or AeroAPI would have on record. Commercial flights don't trust the free historical DBs because the same callsign can fly different specific routings on different days.

---

## Installation

### Prerequisites

- Raspberry Pi with Raspbian (Debian Bookworm or Trixie)
- ADS-B receiver running dump1090, fr24feed, or Virtual Radar Server
- RGB LED Matrix + Adafruit Bonnet (see [Adafruit's guide](https://learn.adafruit.com/adafruit-rgb-matrix-bonnet-for-raspberry-pi/overview))

### Install the RGB Matrix driver

```bash
cd /home/pi
curl https://raw.githubusercontent.com/adafruit/Raspberry-Pi-Installer-Scripts/main/rgb-matrix.sh > /tmp/rgb-matrix.sh
sudo bash /tmp/rgb-matrix.sh
```

### Install FlightTracker

```bash
cd /home/pi
git clone https://github.com/dalarso/FlightTracker
cd FlightTracker
python3 -m venv env
source env/bin/activate
pip install -r requirements.txt
```

Install the RGB Matrix Python bindings into the virtualenv:

```bash
cd /home/pi/rpi-rgb-led-matrix/bindings/python
pip install .
```

### Configure

```bash
cd /home/pi/FlightTracker
cp config.example.py config.py
nano config.py
```

Set your `ZONE_HOME` bounding box, `LOCATION_HOME`, `LOCAL_AIRPORTS`, `RECEIVER_HOST`, and any API keys you have.

### Grant real-time scheduling permission

```bash
sudo setcap 'cap_sys_nice=eip' $(readlink -f $(which python3))
```

> **Note:** `$(which python3)` can fail inside a virtualenv because it resolves to a symlink. `readlink -f` follows the symlink to the real binary, which is what `setcap` requires.

### Set up systemd services

```bash
sudo cp assets/FlightTracker.service    /etc/systemd/system/
sudo cp assets/FlightTrackerWeb.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable FlightTracker.service FlightTrackerWeb.service
sudo systemctl start  FlightTracker.service FlightTrackerWeb.service
```

The web UI will be available at `http://<pi-ip>:5000`.

---

## License

GNU General Public License v3.0 — see [LICENSE](LICENSE).

You're welcome to use, modify, and share this code, but you must keep it under the same license and include proper attribution to the original author, Colin Waddell.

---

## Credits

**Original concept, display hardware integration, and animation engine:**  
[Colin Waddell](https://blog.colinwaddell.com) — [github.com/ColinWaddell/FlightTracker](https://github.com/ColinWaddell/FlightTracker)

**Extended routing intelligence, API stack, web UI, and data layer:**  
This fork — built on top of Colin's foundation with multi-source route resolution, SQLite persistence, and a full fleet management web interface.
