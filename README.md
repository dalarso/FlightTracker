# ✈ FlightTracker — RGB Matrix Flight Display with Intelligent Route Intelligence

> **⚠️ Disclaimer: I am not a developer.** This project was built entirely using [Claude Code](https://claude.ai/code) (Anthropic's AI coding assistant). It works well for my specific setup, but it has not been formally tested, audited, or hardened for general use. **Use at your own risk.** Pull requests and issue reports are welcome, but I may not be able to diagnose or fix problems beyond my own environment.

A heavily extended fork of [Colin Waddell's](https://blog.colinwaddell.com/flight-tracker/) RGB Matrix Flight Tracker. Colin built the original display hardware integration, the animation engine, the ADS-B receiver polling, and the core concept of showing overhead flights on an LED matrix. All of that remains the heart of this project.

**Original project:** [github.com/ColinWaddell/FlightTracker](https://github.com/ColinWaddell/FlightTracker)  
**Colin's blog post:** [blog.colinwaddell.com/flight-tracker](https://blog.colinwaddell.com/flight-tracker/)

> ☢️ Colin has also written about a company selling unauthorized copies of this hardware with a hidden backdoor. [Read his warning here.](https://colinwaddell.com/articles/flight-tracker-led-ripoff-part-2-its-so-much-worse)

---

## What this fork adds

This version takes the original display concept and builds a full flight intelligence stack around it — a multi-tier API routing engine (with a single `_select()` route authority), a SQLite-backed data layer, and a web-based management UI. The goal is to show not just *what* is overhead, but *where it came from and where it's going* — reliably, efficiently, and with minimal API spend. When the sky is quiet the panel falls back to idle scenes — clock, date, and live weather — or an optional **multi-sport live scoreboard**.

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

## Route Resolution — Source Hierarchy + `_select()`

For each overhead flight the route engine asks **FlightRadar24 first, for every flight**, keeps the 7-day route cache and the paid APIs running exactly as before (so credit use doesn't change), and lets a single authority — `_select()` — pick the winning route. The free feeds (OpenSky, adsbdb) are a last resort only. Every result is cached in SQLite to avoid redundant calls.

### Step 0 — Override Rules
User-defined rules stored in SQLite. Pattern-match by callsign (wildcards supported). A full override returns immediately — no API calls are made. Example: `JANET*` → always display "Janet Airlines" departing LAS. A partial rule (one endpoint) skips the paid APIs; FR24, then OpenSky, then adsbdb fill the missing endpoint.

### Step 1 — FlightRadar24 *(free, every flight, by tail number — primary)*
Real-time lookup by aircraft **registration** via the [FlightRadarAPI](https://pypi.org/project/FlightRadarAPI/) library (no API key required). Because it is keyed by the airframe, it answers with the leg that aircraft is flying right now — a reused flight number can't mislead it. The tail comes from the ADS-B feed, the permanent hex → tail cache, or adsbdb's aircraft database; a flight with no known tail can't be looked up (the library has no callsign search). Cached per flight for 1 hour. If FR24 errors (it's an unofficial API and has disappeared before), it backs off for 5 minutes and everything below carries on without it.

### Step 1.5 — Resolved-Route Cache *(scheduled airlines only)*
For airline flights (recognized by ICAO 3-letter prefix), once both endpoints are known, the route is cached for 7 days along with the airport coordinates, so the same flight number later skips the paid APIs. A cached route is validated with a **geometry plausibility check** and busted if the aircraft's position doesn't fit (airlines reuse flight numbers for different city pairs on different days). FR24's answer is still checked against it: if FR24 agrees, the route is credited to FR24; if FR24 has a different complete route touching a home airport, FR24's route is shown and replaces the cache entry.

### Step 1.75 — VRS Live Route Hint *(Virtual Radar Server only)*
When the receiver is Virtual Radar Server, it may supply departure and arrival airport codes directly in the feed. Used when at least one endpoint is a configured local airport and FR24 has no complete route.

### Step 2 — AirLabs Primary Key *(1,000 calls/month free)*
Real-time route lookup by callsign. Returns airport coordinates enabling geometry plausibility checks. Called for every airline flight the resolved cache doesn't already know — FR24's answer doesn't skip it, so the two can be compared and AirLabs is ready when FR24 is down. (For GA / N-number tails a complete FR24 route does skip the paid APIs, as before.) Results cached at the scheduled-airline TTL (7 days) or GA TTL (1 hour). Automatically disables on 402 with 24-hour backoff; resumes immediately when the billing period resets.

### Step 2b — AirLabs Secondary Key *(1,000 calls/month free)*
A second AirLabs API key used as overflow. Fires only when the primary key did not make a live call this invocation (backoff, cache hit, disabled). If the primary returned an empty live response, the secondary is skipped — both keys hit the same backend. Combined, the two keys provide 2,000 free calls/month before AeroAPI spend begins.

### Step 3 — FlightAware AeroAPI *(paid)*
Paid real-time route lookup, capped and tracked monthly. Called when both AirLabs keys have no usable route, or when AirLabs only had a non-local route. Geometry plausibility is applied even on cache hits since the same callsign can fly a different route on a different day.

### Step 4 — The Final Pick — `_select()`
The steps above don't each decide the route on their own; they gather every trusted source's candidate (FR24, AirLabs, AeroAPI) and a single authority, **`_select()`**, picks the winner. It ranks candidates by **route tier** (a route touching a home airport beats one that doesn't; a complete route beats a partial one), then by a fixed **source priority** — FR24 first, then AirLabs, AeroAPI — and rejects any candidate whose geometry is implausible for the aircraft's current position. So when FR24 and a paid API disagree on equal footing, the board shows FR24's route.

### Step 5 — Last Resort: OpenSky, then adsbdb
Only when none of the trusted sources has a usable route. **OpenSky** (free, by hex) knows the departure airport of a flight in progress but rarely the destination; **adsbdb** (free, by callsign) is a static historical DB — a flight number's old city pair — so it is asked only if OpenSky has nothing either. Neither is queried for flights the trusted sources resolve.

### Departure Veto
Some flight numbers fly more than one leg (airline turns like CLT→LAS→CLT, through-flights), and AirLabs sometimes answers with the leg that *ended* at the home airport while the plane is climbing out of it — a departure shown as `ORD→LAS`. When the plane is evidently **departing** a local airport — climbing at ≥ 300 ft/min, or OpenSky's live departure airport is local and the plane isn't descending — any candidate that *arrives* at a local airport from elsewhere is dropped before `_select()` runs (and a cached one is busted). OpenSky is asked for that evidence only when a candidate route has that inbound shape and the plane isn't descending — a few lookups a day.

### Paid-Miss Cache
When both AirLabs and AeroAPI return empty for the same callsign, a 2-hour suppression entry is written. Prevents repeated quota burns on GA or obscure flights that will never have a filed route.

### Cache TTL Reference

| Cache type | TTL | Notes |
|---|---|---|
| FR24 result | 1 hour per flight / 5 min miss | Keyed by callsign, so tomorrow's flight with the same number is asked again |
| Resolved commercial route | 7 days | Set by `ROUTE_TTL_SCHEDULED` — once origin+destination confirmed; geometry-validated on read, checked against FR24 |
| AirLabs / AeroAPI result | 7 days (commercial) / 1 hour (GA) | Same TTL as resolved route when committed |
| OpenSky result | 1 hour | Only when asked (last resort or departure evidence) |
| adsbdb route result | 1 hour | Only when asked (absolute last resort) |
| Paid-miss (no route found) | 2 hours | Suppresses redundant paid API calls for unresolvable callsigns |
| Aircraft type / registration | Indefinite | Hardware doesn't change — permanent mapping |
| AirLabs 402 backoff | Until billing period resets | Clears automatically on reset day; no restart needed |
| FR24 error backoff | 5 minutes | The paid APIs and caches carry on meanwhile |

TTLs can be overridden in `config.py` — see `ROUTE_TTL_SCHEDULED`, `ROUTE_TTL_DEFAULT`, and the other cache TTL constants.

### FR24 vs Paid APIs
Whenever FR24 answers live and a paid API (or the resolved cache) also has the route for the same flight, the pair is stored in the `fr24_checks` table. The Stats page shows how often they agree, split by what FR24 was compared against, with the recent disagreements side by side so you can judge which one was right.

---

## Aircraft Type Resolution

Parallel to route lookup, a separate thread resolves the aircraft type for display and logging:

1. **airplanes.live** — returns type code and registration in one call
2. **adsbdb** aircraft endpoint — type fallback if airplanes.live misses; also the registration source (hex → tail, cached permanently) when the feed and airplanes.live don't carry one
3. **airplanes.live /v2/reg** — registration-only fallback
4. **FlightRadar24** — `get_flights(registration=…)` as a free last resort by tail number

(OpenSky's `/metadata/aircraft` endpoint used to provide registrations; it now answers `410 Gone` and is no longer used.)

Type codes (e.g. `B738`) are translated to human-readable names (e.g. `Boeing 737-800`) via a built-in lookup table covering airliners, regional jets, business jets, GA aircraft, and helicopters.

---

## Live Scoreboard *(optional)*

When no aircraft is overhead, the panel can show a **live sports scoreboard** instead of the idle clock / date / weather. It supports **NHL, NFL, MLB, NBA, WNBA, MLS, and the FIFA World Cup** simultaneously — each configured with its own team and toggled independently. The board takes over the moment a configured team's game goes live, yields back to overhead flights (flights always win), and returns to the idle scenes once the game is over.

- **Free data sources, no keys** — NHL (`api-web.nhle.com`), MLB (`statsapi.mlb.com`), and NFL / NBA / WNBA / MLS / **FIFA World Cup** (ESPN's public API — e.g. `basketball/wnba`, `soccer/fifa.world`; follow any team or nation).
- **Priority** — if several configured teams are live at once, `SCOREBOARD_PRIORITY` decides which is shown; a LIVE game always beats a finished one. The LED scene and the web `/api/scoreboard` endpoint pick the game through one shared selector, so they can't disagree.
- **Standard layout** — away team on the left, home team on the right (as on any broadcast scoreboard), with your team highlighted in gold on whichever side it plays. The bottom line shows the period plus the game clock while play is live (`2nd 14:32`, `Q4 0:45`, `2nd 67'`), the period alone during breaks, and `FINAL` / `FINAL OT` / `FINAL 2OT` / `FINAL SO` / `FINAL AET` / `FINAL PEN` / `F/10` at the end. The web UI's "On the board now" line and the desktop goal-horn app use the same away-left / home-right order.
- **Celebrations** — a full-screen scroll on a goal/score (`SCOREBOARD_GOAL_CELEBRATION_SECONDS`), and a one-shot "{TEAM} WINS!" at the final (`SCOREBOARD_WIN_CELEBRATION_SECONDS`; both editable in the web UI, 0 = off). A plane overhead still wins — the scroll pauses and resumes after it. Optionally fires a fire-and-forget **LAN goal-horn** UDP packet (`SCOREBOARD_GOAL_HORN_HOST`) to a desktop listener that plays a horn in sync with the board — for your NHL team only (other sports celebrate on the panel but never sound the horn).
- **Final at the horn** — the NHL API reports a "soft final" (`OVER`) for a few minutes before `FINAL`; it is treated as final, so the score and WIN celebration appear at the horn instead of after a detour back to the idle screens. Postponed / cancelled / suspended games (and MLB warmups) never take over the board.
- **Post-game** — stays up for `SCOREBOARD_POST_GAME_MINUTES` after the final, then idle scenes resume. The web UI follows the same window (`on_board` in `/api/scoreboard`).

Enable with `SCOREBOARD_ENABLED = True` and set each sport's team ID/name — see the config block below.

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

**`fr24_checks`** — per-flight record of FR24's route next to the paid APIs' (or the route cache's), and whether they agreed. Powers the FR24 vs paid APIs card on the stats page. (Older databases may still hold the retired `free_api_checks` / `ga_free_api_checks` tables; nothing reads them.)

---

## Web UI — `server.py` (port 5000)

A Flask application running as a separate systemd service (`FlightTrackerWeb.service`). Provides:

### Security & network exposure

**Trust model (read this):** the dashboard is single-user, **LAN-only, and has no authentication** by design. The header/Origin checks below are **anti-CSRF, not a login** — anyone who can reach `:5000` on your network has full control (toggle night mode, edit config, restart the service). That's an acceptable model for a home LAN behind a router; it is **not** safe to expose to the internet. Harden it for your network:

- **Binds `0.0.0.0:5000`** (reachable from any device on your LAN). Keep the Pi behind your router/firewall — do **not** port-forward `:5000` to the internet.
- **API keys are masked by default:** `GET /api/config` returns placeholders, not the real keys — they're never auto-loaded into the page. The eye button next to a key reveals that one key on demand (one request per field).
- **CSRF-guarded, two layers:** every state-changing request must carry an `X-Requested-With: FlightTracker` header (the dashboard sends it automatically), so a malicious page you happen to visit can't drive the dashboard through your browser. In addition, a request whose browser `Origin` resolves to a **non-LAN** host is rejected — this blocks DNS-rebinding (where a public name is rebound to the Pi's LAN IP), which the static header alone can't catch. LAN/loopback/`*.local`/bare-hostname origins (and header-less clients like `curl`) are allowed. **Tailscale is fully supported** for remote access — both the CGNAT IP range (`100.64.0.0/10`) and MagicDNS names (`*.ts.net`) pass, so you can reach the dashboard from anywhere on your tailnet.
- **Health endpoint:** `GET /api/health` returns a liveness snapshot the display process writes each poll — `last_poll_age_sec`, `processing`, `active_threads`, `cache_write_failures`, `uptime_sec`, and an `ok` flag. Point an uptime monitor at it to catch a wedged poll or a failing SD card (rising `cache_write_failures`) before the panel visibly goes stale.
- **Production server:** served by [`waitress`](https://pypi.org/project/waitress/) when installed (it's in `requirements.txt`); falls back to the Flask dev server otherwise.
- **Service control needs sudo** (the Save & Restart and service buttons run `systemctl`). Grant a **narrow** rule — never `NOPASSWD: ALL`:
  ```
  # sudo visudo -f /etc/sudoers.d/flighttracker
  pi ALL=(root) NOPASSWD: /usr/bin/systemctl start FlightTracker, /usr/bin/systemctl stop FlightTracker, /usr/bin/systemctl restart FlightTracker
  ```

### Layout
- One responsive page with no build step: tabs across the top on a desktop, a bottom tab bar on a phone, light and dark themes (follows the OS; the sun/moon button overrides it). Icons are an inline SVG sprite, so there are no emoji and no icon font.
- **Header:** a connection badge (green when the Pi answers, amber when the display self-check in `/api/health` reports a problem, red when the Pi is unreachable) and the Display / Night / Service / APIs switches, inline on wide screens and under the power button on smaller ones. The switches stay disabled until the Pi has reported their real state.
- Tabs: Stats (home), Map, APIs, Rules, Settings, Log. The open tab is kept in the URL (`#map`, `#config`, …) so a reload or a bookmark lands on it. Polling pauses while the page is hidden.

### Settings
Live editing of `config.py` without SSH, in cards with a section index: zone, altitude filter, home location and timezone, display hardware, weather, scoreboard, receiver, API keys (masked), billing limits and reset days, and cache lifetimes. A sticky save bar shows when the form differs from what's saved, the browser warns before you leave with unsaved edits, and impossible values (an upside-down zone, minimum altitude above maximum) are flagged before anything is sent. **Save & restart display** applies the settings that need a restart. Footer links go to this repo and Colin Waddell's original project.

### Map
A Leaflet map (loaded the first time the tab opens, pinned with Subresource Integrity) showing the zone rectangle and home. Drag the corner handles to resize the zone, then **Save zone**.

### APIs
- Usage meters for AirLabs 1, AirLabs 2 and FlightAware AeroAPI with billing periods, plus a manual correction if a period was mis-counted
- The route data stack in the order a route is trusted (FR24, AirLabs 1 and 2, AeroAPI, then OpenSky and adsbdb as the last resort), with per-source enable/disable (flag files in `/tmp/`, cleared on reboot), cache entry counts and per-source cache clearing
- Clear a single cache entry by tail number or callsign

### Stats (the home tab)
Refreshed every minute while it's open:
- **Today:** flights so far against the same time yesterday, distinct aircraft, the 7-day daily average, paid lookups, flights by hour, and the top airlines (N-number registrations grouped into one "N-reg" row)
- **Recent flights**, newest first ("12 min ago" within the last hour; new arrivals flash), and **history search** by flight number, tail or airport
- **Period:** a preset (yesterday, 7, 30 or 90 days) or a custom range, with a daily chart (7-day average line, tap or hover for the day's numbers), totals, the busiest day, distinct aircraft, top airlines, tails, routes and aircraft types, busiest hours, where routes came from (which API supplied each displayed route: FR24, AirLabs, AeroAPI, the last-resort feeds, overrides), and a daily table
- **FR24 vs paid APIs**: how often FR24's route agreed with AirLabs, AeroAPI or the route cache for the same flight, with a 30-day daily trend and the recent disagreements side by side

The data stays in SQLite (`ft_flights.db`, WAL mode). Days before today never change, so the server aggregates them once and caches the result; each request re-reads only today's rows, and a whole response is reused until the database changes. On the Pi Zero 2 W that took the 90-day view from about 0.75 s to about 60 ms, and search from 0.34 s to 0.14 s (with a `seen_at` index the web service adds at start-up).

### Rules
- **Test a flight lookup:** run the full resolution stack for any callsign or tail (cached, or live-only) and see what each API returned and what the display shows
- **Override rules:** add, edit, delete and reorder (drag on a desktop, arrow buttons anywhere); wildcard patterns (e.g. `JANET*`); per-flight display name, aircraft type, origin, destination and note. Changes take effect on the next poll via version-counter cache invalidation.

### Log
The live log stream with a text filter, an auto-scroll that stops following while you're scrolled up, and a line count.

---

## Pause & Night Mode

Two flag-file controls let you manage the display without restarting the service:

| Flag file | Effect |
|---|---|
| `/tmp/ft_paused` | Blanks the matrix (brightness → 0) while the file exists |
| `/tmp/ft_night` | Drops brightness to `NIGHT_BRIGHTNESS` while the file exists |

The web UI header has Display and Night switches for both (real buttons: Tab to one, Enter or Space flips it). Flag files are in `/tmp/` so they auto-clear on reboot — the display always comes back at full brightness after a power cycle.

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
WEATHER_LOCATION    = "your city,state,us"    # encoded automatically — plain text is fine
OPENWEATHER_API_KEY = ""          # free tier at openweathermap.org (optional)
TEMPERATURE_UNITS   = "imperial"  # "imperial" (°F) or "metric" (°C)
RAINFALL_ENABLED    = False       # experimental — requires local taps-aff service

# ── Scoreboard (optional multi-sport live score — see "Live Scoreboard" above) ─
SCOREBOARD_ENABLED        = False                          # master switch
SCOREBOARD_PRIORITY       = ["NHL", "NFL", "MLB", "NBA", "WNBA", "MLS", "FIFA"]
SCOREBOARD_NHL_ENABLED    = True
SCOREBOARD_NHL_TEAM_ID    = 0                              # your team's numeric NHL id
SCOREBOARD_NHL_TEAM_NAME  = "NHL"                          # ≤4 chars, shown on the LED
# NFL / MLB / NBA / WNBA / MLS / FIFA each take the same _ENABLED / _TEAM_ID / _TEAM_NAME trio
# (FIFA = the World Cup — set SCOREBOARD_FIFA_TEAM_ID to your nation, e.g. 660 = USA;
#  WNBA team IDs e.g. 17 = Las Vegas Aces, 5 = Indiana Fever)
SCOREBOARD_GOAL_HORN_HOST = ""                             # e.g. "192.168.1.30"; "" = off

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
# ROUTE_MISS_TTL       = 300      # No-route suppression (default 5 min)
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

## Testing

The repo ships a test suite (hardware-free — the LED matrix is stubbed, and a temp `FT_DATA_DIR` keeps the real DB untouched). It covers the route-resolution cascade end-to-end (a scenario battery asserting on call counts, cache writes, and the chosen route), the scoreboard selection, the web layer, and the display scenes.

```bash
source env/bin/activate
pip install -r requirements-test.txt
python -m pytest tests/ -q
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

| Callsign type | FR24 | AirLabs | AeroAPI | OpenSky | adsbdb |
|---|---|---|---|---|---|
| **Commercial** (SWA, DAL, etc.) | ✅ Every flight, wins ties | ✅ Unless the route cache knows the flight | ✅ When AirLabs has nothing usable | Last resort | Absolute last resort |
| **GA / charter** (N-number tails) | ✅ Every flight; a complete route skips the paid APIs | When FR24 has no complete route* | When FR24 has no complete route* | Last resort | Absolute last resort |
| **Military/Gov** (RCH, SAM…) | ✅ When the tail is known | ⛔ Skipped | ⛔ Skipped | Last resort | Absolute last resort |
| **Partial override** | ✅ Fills the blank | ⛔ Skipped | ⛔ Skipped | Fills the blank | Fills the blank |
| **Full override** | ⛔ Skipped | ⛔ Skipped | ⛔ Skipped | ⛔ Skipped | ⛔ Skipped |

\* N-number callsigns (`N12345`) skip the paid APIs entirely — VFR GA flights don't file the instrument flight plans AirLabs or AeroAPI would have on record. OpenSky and adsbdb are asked only when FR24, AirLabs and AeroAPI all have no usable route (OpenSky is also asked for departure evidence when a paid route looks like the previous, inbound leg).

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
