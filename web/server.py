import calendar
import json
import logging
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template, request, Response, stream_with_context

# Silence Werkzeug's request log and startup banner — they pollute plane.log
# with HTTP noise that makes flight data harder to read.
logging.getLogger("werkzeug").setLevel(logging.ERROR)

_PACIFIC = ZoneInfo("America/Los_Angeles")

LOG_PATH = Path.home() / "plane.log"


def _log(msg):
    # Write to stdout — captured by systemd's StandardOutput=append:/home/pi/plane.log,
    # same as overhead.py.  Avoids a direct file-write that could race with log rotation.
    ts = datetime.now(_PACIFIC).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True

BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH      = BASE_DIR / "config.py"
PAUSE_FLAG       = Path("/tmp/ft_paused")
NIGHT_FLAG       = Path("/tmp/ft_night")
APIS_DISABLED_FLAG    = Path("/tmp/ft_apis_disabled")
ADSBDB_DISABLED_FLAG  = Path("/tmp/ft_adsbdb_disabled")
OPENSKY_DISABLED_FLAG = Path("/tmp/ft_opensky_disabled")
AIRLABS_DISABLED_FLAG  = Path("/tmp/ft_airlabs_disabled")
AIRLABS2_DISABLED_FLAG = Path("/tmp/ft_airlabs2_disabled")
AEROAPI_DISABLED_FLAG  = Path("/tmp/ft_aeroapi_disabled")

_API_FLAGS: dict[str, Path] = {
    "adsbdb":      ADSBDB_DISABLED_FLAG,
    "opensky":     OPENSKY_DISABLED_FLAG,
    "airlabs":     AIRLABS_DISABLED_FLAG,
    "airlabs2":    AIRLABS2_DISABLED_FLAG,
    "flightaware": AEROAPI_DISABLED_FLAG,
}
FLIGHT_DATA_FILE = Path("/tmp/ft_data.json")

AIRLABS_USAGE_FILE  = BASE_DIR / "airlabs_usage.json"
AIRLABS2_USAGE_FILE = BASE_DIR / "airlabs2_usage.json"
AEROAPI_USAGE_FILE  = BASE_DIR / "aeroapi_usage.json"
OVERRIDES_FILE     = BASE_DIR / "ft_overrides.json"
DB_FILE            = BASE_DIR / "ft_flights.db"

AIRLABS_MONTHLY_LIMIT = 1000    # free tier cap

# Airline prefix → full name (mirrors _AIRLINE_NAMES in overhead.py)
AIRLINE_NAMES: dict[str, str] = {
    "AAL": "American Airlines",   "DAL": "Delta Air Lines",
    "UAL": "United Airlines",     "SWA": "Southwest Airlines",
    "ASA": "Alaska Airlines",     "JBU": "JetBlue Airways",
    "NKS": "Spirit Airlines",     "FFT": "Frontier Airlines",
    "SCX": "Sun Country Airlines","AAY": "Allegiant Air",
    "HAL": "Hawaiian Airlines",   "VRD": "Virgin America",
    "MXY": "Breeze Airways",      "VXP": "Avelo Airlines",
    "ROU": "Air Canada Rouge",
    "LXJ": "Flexjet",             "JRE": "flyExclusive",
    "JSX": "JSX",                 "TWY": "Solarius Aviation",
    "JAN": "Janet Airlines",
    "FDX": "FedEx Express",       "UPS": "UPS Airlines",
    "GTI": "Atlas Air",           "ABX": "ABX Air",
    "ASN": "Amazon Air",          "PAC": "Polar Air Cargo",
    "CKS": "Kalitta Air",         "WGN": "Western Global Airlines",
    "NCR": "Northern Air Cargo",  "SOU": "Southern Air",
    "DHK": "DHL Aviation",        "AGX": "Amerijet International",
    "OAE": "Omni Air International",
    "OCN": "Discover Airlines",
    "ACA": "Air Canada",          "WJA": "WestJet",
    "POE": "Porter Airlines",     "FLE": "Flair Airlines",
    "SWG": "Sunwing Airlines",
    "AMX": "Aeroméxico",          "VOI": "Volaris",
    "VIV": "VivaAerobus",
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
    "KAL": "Korean Air",          "QFA": "Qantas",
    "ANA": "All Nippon Airways",  "JAL": "Japan Airlines",
    "CPA": "Cathay Pacific",      "EVA": "EVA Air",
    "CCA": "Air China",           "CSN": "China Southern",
    "ANZ": "Air New Zealand",
    "CMP": "Copa Airlines",       "AVA": "Avianca",
    "SKW": "SkyWest Airlines",    "ENY": "Envoy Air",
    "RPA": "Republic Airways",    "QXE": "Horizon Air",
    "ASH": "Mesa Airlines",       "PDT": "Piedmont Airlines",
    "JIA": "PSA Airlines",        "UCA": "CommutAir",
    "CPZ": "Comair",              "MTN": "Mountain Air Cargo",
    "FLG": "Frontier (charter)",
}
AEROAPI_COST_PER_CALL = 0.005   # $0.005 per call
FEEDER_MONTHLY_CREDIT = 10.00   # FlightAware feeder credit
AIRLABS_RESET_DAY     = 9       # AirLabs billing period resets on the 9th
AEROAPI_RESET_DAY     = 1       # FlightAware credit resets on the 1st

# Keys written by write_config — used to round-trip unknown keys safely
_KNOWN_KEYS = {
    "ZONE_HOME", "LOCATION_HOME", "WEATHER_LOCATION", "OPENWEATHER_API_KEY",
    "TEMPERATURE_UNITS", "MIN_ALTITUDE", "MAX_ALTITUDE", "BRIGHTNESS",
    "GPIO_SLOWDOWN", "NIGHT_BRIGHTNESS", "TIMEZONE", "JOURNEY_CODE_SELECTED",
    "JOURNEY_BLANK_FILLER", "HAT_PWM_ENABLED", "RECEIVER_HOST", "LOCAL_AIRPORTS",
    "LOCAL_AIRPORT",  # deprecated single-value key — kept here so it's NOT written as an "extra key" when migrating old configs
    "OPENSKY_CLIENT_ID", "OPENSKY_CLIENT_SECRET", "FLIGHTAWARE_API_KEY",
    "AIRLABS_API_KEY", "AIRLABS_API_KEY_2",
    # Display extras
    "LOADING_LED_ENABLED", "LOADING_LED_GPIO_PIN", "RAINFALL_ENABLED",
    # Billing tracking
    "FEEDER_MONTHLY_CREDIT",
    "RECEIVER_TYPE",
    "TIME_FORMAT", "DATE_FORMAT",
    "AIRLABS_MONTHLY_LIMIT", "AIRLABS_RESET_DAY",
    "AIRLABS2_MONTHLY_LIMIT", "AIRLABS2_RESET_DAY",
    "AEROAPI_RESET_DAY",
    # Cache TTLs
    "ADSBDB_CACHE_TTL", "OPENSKY_CACHE_TTL",
    "ROUTE_TTL_SCHEDULED", "ROUTE_TTL_DEFAULT", "ROUTE_MISS_TTL", "ROUTE_PAID_MISS_TTL",
}

# Secret/key fields that must never be overwritten with an empty string.
# The UI populates these from the loaded config, but if the page loads in a
# degraded state (e.g. config read failed) the field is blank — saving it would
# wipe the real key stored on disk.
_SENSITIVE_KEYS = {
    "OPENWEATHER_API_KEY", "OPENSKY_CLIENT_SECRET",
    "FLIGHTAWARE_API_KEY", "AIRLABS_API_KEY", "AIRLABS_API_KEY_2",
}


def read_config():
    """Execute config.py in a sandboxed namespace and return its variables."""
    safe_globals = {"__builtins__": {}}
    with open(CONFIG_PATH) as f:
        exec(compile(f.read(), str(CONFIG_PATH), "exec"), safe_globals)
    return {k: v for k, v in safe_globals.items() if not k.startswith("_")}


# Update timezone from config — _log() looks up _PACIFIC at call time so this applies immediately
try:
    _PACIFIC = ZoneInfo(read_config().get("TIMEZONE", "America/Los_Angeles"))
except Exception:
    pass


def write_config(data):
    """
    Write config.py. Preserves any keys not managed by the web UI
    by reading the current config first and overlaying the new values.
    """
    # Load existing config so unknown keys aren't lost.
    # If the file doesn't exist (fresh install) start from defaults.
    # If the file EXISTS but can't be parsed, abort rather than overwriting
    # good config values with empty/default ones — e.g. a transient I/O error
    # or syntax error should not silently blank all API keys.
    try:
        existing = read_config()
    except FileNotFoundError:
        existing = {}
    except Exception as e:
        raise ValueError(f"config.py could not be read: {e} — save aborted to prevent data loss")

    # Overlay only the known managed keys from the POST body.
    # For sensitive keys, skip an empty value so the client can't accidentally
    # blank a real credential by saving a page that loaded in a degraded state.
    for k in _KNOWN_KEYS:
        if k in data:
            if k in _SENSITIVE_KEYS and not data[k] and existing.get(k):
                continue  # keep the existing non-empty value
            existing[k] = data[k]

    # Validate TIMEZONE before writing — an invalid string would break the service on restart
    tz_str = str(existing.get("TIMEZONE", "America/Los_Angeles"))
    try:
        ZoneInfo(tz_str)
    except Exception:
        existing["TIMEZONE"] = "America/Los_Angeles"

    zone = existing.get("ZONE_HOME") or data.get("ZONE_HOME")
    loc  = existing.get("LOCATION_HOME") or data.get("LOCATION_HOME")
    if not zone or not loc:
        raise ValueError("ZONE_HOME and LOCATION_HOME are required")

    content = f"""ZONE_HOME = {{
    "tl_y": {float(zone["tl_y"])},
    "tl_x": {float(zone["tl_x"])},
    "br_y": {float(zone["br_y"])},
    "br_x": {float(zone["br_x"])}
}}
LOCATION_HOME = [
    {float(loc[0])},
    {float(loc[1])},
    {float(loc[2])}
]
WEATHER_LOCATION = {repr(str(existing.get("WEATHER_LOCATION", "")))}
OPENWEATHER_API_KEY = {repr(str(existing.get("OPENWEATHER_API_KEY", "")))}
TEMPERATURE_UNITS = {repr(str(existing.get("TEMPERATURE_UNITS", "imperial")))}
MIN_ALTITUDE = {int(existing.get("MIN_ALTITUDE", 100))}
MAX_ALTITUDE = {int(existing.get("MAX_ALTITUDE", 15000))}
BRIGHTNESS = {int(existing.get("BRIGHTNESS", 80))}
GPIO_SLOWDOWN = {int(existing.get("GPIO_SLOWDOWN", 2))}
NIGHT_BRIGHTNESS = {int(existing.get("NIGHT_BRIGHTNESS", 20))}
JOURNEY_CODE_SELECTED = {repr(str(existing.get("JOURNEY_CODE_SELECTED", "")))}
JOURNEY_BLANK_FILLER = {repr(str(existing.get("JOURNEY_BLANK_FILLER", " ? ")))}
TIME_FORMAT = {repr(str(existing.get("TIME_FORMAT", "24h")))}
DATE_FORMAT = {repr(str(existing.get("DATE_FORMAT", "MDY")))}
HAT_PWM_ENABLED = {bool(existing.get("HAT_PWM_ENABLED", True))}

RECEIVER_HOST = {repr(str(existing.get("RECEIVER_HOST", "localhost")))}
RECEIVER_TYPE = {repr(str(existing.get("RECEIVER_TYPE", "dump1090")))}

LOCAL_AIRPORTS = {repr(str(existing.get("LOCAL_AIRPORTS", existing.get("LOCAL_AIRPORT", ""))))}
OPENSKY_CLIENT_ID = {repr(str(existing.get("OPENSKY_CLIENT_ID", "")))}
OPENSKY_CLIENT_SECRET = {repr(str(existing.get("OPENSKY_CLIENT_SECRET", "")))}
FLIGHTAWARE_API_KEY = {repr(str(existing.get("FLIGHTAWARE_API_KEY", "")))}
AIRLABS_API_KEY = {repr(str(existing.get("AIRLABS_API_KEY", "")))}
AIRLABS_API_KEY_2 = {repr(str(existing.get("AIRLABS_API_KEY_2", "")))}
TIMEZONE = {repr(str(existing.get("TIMEZONE", "America/Los_Angeles")))}
LOADING_LED_ENABLED = {bool(existing.get("LOADING_LED_ENABLED", False))}
LOADING_LED_GPIO_PIN = {int(existing.get("LOADING_LED_GPIO_PIN", 25))}
RAINFALL_ENABLED = {bool(existing.get("RAINFALL_ENABLED", False))}
FEEDER_MONTHLY_CREDIT = {float(existing.get("FEEDER_MONTHLY_CREDIT", 10.00))}
AIRLABS_MONTHLY_LIMIT = {int(existing.get("AIRLABS_MONTHLY_LIMIT", 1000))}
AIRLABS_RESET_DAY = {int(existing.get("AIRLABS_RESET_DAY", 9))}
AIRLABS2_MONTHLY_LIMIT = {int(existing.get("AIRLABS2_MONTHLY_LIMIT", 1000))}
AIRLABS2_RESET_DAY = {int(existing.get("AIRLABS2_RESET_DAY", 9))}
AEROAPI_RESET_DAY = {int(existing.get("AEROAPI_RESET_DAY", 1))}
ADSBDB_CACHE_TTL = {int(existing.get("ADSBDB_CACHE_TTL", 3600))}
OPENSKY_CACHE_TTL = {int(existing.get("OPENSKY_CACHE_TTL", 3600))}
ROUTE_TTL_SCHEDULED = {int(existing.get("ROUTE_TTL_SCHEDULED", 604800))}
ROUTE_TTL_DEFAULT = {int(existing.get("ROUTE_TTL_DEFAULT", 3600))}
ROUTE_MISS_TTL = {int(existing.get("ROUTE_MISS_TTL", 300))}
ROUTE_PAID_MISS_TTL = {int(existing.get("ROUTE_PAID_MISS_TTL", 7200))}
"""
    # Preserve any extra keys not managed by this template (e.g. custom user keys)
    for k, v in existing.items():
        if k not in _KNOWN_KEYS and not k.startswith("_"):
            content += f"{k} = {repr(v)}\n"

    # Atomic write: write to a temp file then rename so a crash mid-write
    # can't leave config.py truncated or empty.
    tmp = str(CONFIG_PATH) + ".tmp"
    with open(tmp, "w") as f:
        f.write(content)
    os.replace(tmp, CONFIG_PATH)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/config", methods=["GET"])
def get_config():
    try:
        return jsonify(read_config())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/config", methods=["POST"])
def post_config():
    try:
        data = request.json
        if not isinstance(data, dict):
            return jsonify({"error": "Invalid payload"}), 400
        write_config(data)
        if data.get("restart"):
            # Run in a daemon thread so the HTTP response returns immediately,
            # but log any sudo/systemctl failure rather than silently ignoring it.
            import threading
            def _do_restart():
                try:
                    subprocess.run(
                        ["sudo", "/usr/bin/systemctl", "restart", "FlightTracker"],
                        check=True, capture_output=True, timeout=15,
                    )
                    _log("[web] service restarted via config save")
                except Exception as e:
                    _log(f"[web] ERROR: service restart failed: {e}")
            threading.Thread(target=_do_restart, daemon=True).start()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@app.route("/api/display/night", methods=["POST"])
def display_night():
    if NIGHT_FLAG.exists():
        NIGHT_FLAG.unlink()
        _log("[web] night mode off")
    else:
        NIGHT_FLAG.touch()
        _log("[web] night mode on")
    return jsonify({"ok": True, "night": NIGHT_FLAG.exists()})


@app.route("/api/apis/toggle", methods=["POST"])
def apis_toggle():
    """Toggle an API on or off.

    Body: { "api": "adsbdb"|"opensky"|"airlabs"|"flightaware" }
    Omit body (or send {}) to toggle the combined paid-API kill-switch (legacy).
    """
    data = request.json or {}
    api  = data.get("api", "").lower()

    if api in _API_FLAGS:
        flag = _API_FLAGS[api]
        if flag.exists():
            flag.unlink()
            _log(f"[web] {api} enabled")
        else:
            flag.touch()
            _log(f"[web] {api} disabled")
        return jsonify({
            "ok":      True,
            "api":     api,
            "enabled": not flag.exists(),
        })

    # Legacy: no api → toggle the combined kill-switch
    if APIS_DISABLED_FLAG.exists():
        APIS_DISABLED_FLAG.unlink()
        _log("[web] limited APIs enabled")
    else:
        APIS_DISABLED_FLAG.touch()
        _log("[web] limited APIs disabled")
    return jsonify({"ok": True, "apis_disabled": APIS_DISABLED_FLAG.exists()})


@app.route("/api/cache/clear", methods=["POST"])
def cache_clear():
    """Delete cached route entries for one API (or all).

    Body: { "api": "adsbdb"|"opensky"|"airlabs"|"flightaware"|"resolved"|"all" }
    Returns: { "ok": true, "deleted": N }
    """
    data = request.json or {}
    api  = (data.get("api") or "").lower()

    # Cache keys for airlabs1 are 'airlabs:{cs}'; for airlabs2 'airlabs2:{cs}'.
    # Source-based LIKE must distinguish them — 'airlabs' is a substring of 'airlabs2'
    # so airlabs clear uses NOT LIKE '%airlabs2%' to avoid over-clearing.
    _source_map = {
        "adsbdb":      "DELETE FROM cache WHERE cache_type='route' AND source LIKE '%adsbdb%'",
        "opensky":     "DELETE FROM cache WHERE cache_type='route' AND source LIKE '%opensky%'",
        "airlabs":     "DELETE FROM cache WHERE cache_type='route' AND "
                       "(key LIKE 'airlabs:%' OR (source LIKE '%airlabs%' AND source NOT LIKE '%airlabs2%'))",
        "airlabs2":    "DELETE FROM cache WHERE cache_type='route' AND "
                       "(key LIKE 'airlabs2:%' OR source LIKE '%airlabs2%')",
        "flightaware": "DELETE FROM cache WHERE cache_type='aeroapi'",
        "resolved":    "DELETE FROM cache WHERE cache_type='resolved'",
        "all":         "DELETE FROM cache WHERE cache_type IN ('route','aeroapi','resolved')",
    }
    if api not in _source_map:
        return jsonify({"error": f"Unknown api '{api}'. Valid: {', '.join(_source_map)}"}), 400

    conn = None
    try:
        conn = sqlite3.connect(str(DB_FILE))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        deleted = conn.execute(_source_map[api]).rowcount
        conn.commit()
        _log(f"[web] cache cleared for '{api}': {deleted} entries removed")
        return jsonify({"ok": True, "deleted": deleted})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@app.route("/api/cache/clear/entry", methods=["POST"])
def cache_clear_entry():
    """Clear cached data for a specific tail number (registration) or callsign.

    Tail number  → deletes aircraft-type and reg entries for that hex code.
    Callsign     → deletes route, aeroapi, resolved, and paid_miss entries.
    Both are attempted so a single call handles ambiguous inputs.

    Body:    { "value": "N9003B" }  or  { "value": "SWA1230" }
    Returns: { "ok": true, "deleted": N, "found_as": "aircraft type|route|..." }
    """
    data  = request.json or {}
    value = (data.get("value") or "").strip().upper()
    if not value:
        return jsonify({"error": "value required"}), 400

    conn = None
    deleted  = 0
    found_as = []
    try:
        conn = sqlite3.connect(str(DB_FILE))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")

        # 1. Treat as registration — find hex code(s) via the reg cache, then
        #    purge both the aircraft-type entry and the reg mapping itself.
        hex_rows = conn.execute(
            "SELECT key FROM cache WHERE cache_type='reg' AND UPPER(value)=?", (value,)
        ).fetchall()
        for (hex_code,) in hex_rows:
            n = conn.execute(
                "DELETE FROM cache WHERE cache_type IN ('aircraft','reg') AND key=?",
                (hex_code,)
            ).rowcount
            deleted += n
            if n:
                found_as.append("aircraft type")

        # 2. Treat as callsign — purge route, aeroapi, resolved, paid_miss.
        #    Keys may be bare (SWA1230), airlabs-prefixed (airlabs:SWA1230),
        #    or airlabs2-prefixed (airlabs2:SWA1230).
        route_deleted = 0
        for key in [value, f"airlabs:{value}", f"airlabs2:{value}"]:
            route_deleted += conn.execute(
                "DELETE FROM cache WHERE UPPER(key)=? "
                "AND cache_type IN ('route','aeroapi','resolved','paid_miss')",
                (key.upper(),)
            ).rowcount
        deleted += route_deleted
        if route_deleted:
            found_as.append("route")

        conn.commit()
        what = " + ".join(found_as) if found_as else "nothing matched"
        _log(f"[web] cache entry cleared for '{value}': {deleted} entries removed ({what})")
        return jsonify({"ok": True, "deleted": deleted, "found_as": what})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@app.route("/api/cache/stats", methods=["GET"])
def cache_stats():
    """Return the count of live (non-expired) cache entries per API.

    Composite-source entries (e.g. 'adsbdb+opensky') are counted in every
    participating API bucket so the totals reflect true cache coverage.
    """
    now = int(time.time())
    try:
        conn = sqlite3.connect(str(DB_FILE))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT cache_type, source, COUNT(*) cnt FROM cache WHERE expires_at>? GROUP BY cache_type, source",
            (now,)
        ).fetchall()
        conn.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    counts = {"adsbdb": 0, "opensky": 0, "airlabs": 0, "airlabs2": 0, "flightaware": 0, "resolved": 0}
    for r in rows:
        ct, src = r["cache_type"], r["source"] or ""
        cnt = r["cnt"]
        if ct == "aeroapi":
            counts["flightaware"] += cnt
        elif ct == "resolved":
            counts["resolved"] += cnt
        elif ct == "route":
            # Split on '+' for exact component matching — avoids 'airlabs' matching 'airlabs2'
            parts = set(src.split("+"))
            if "adsbdb"   in parts: counts["adsbdb"]   += cnt
            if "opensky"  in parts: counts["opensky"]  += cnt
            if "airlabs"  in parts: counts["airlabs"]  += cnt
            if "airlabs2" in parts: counts["airlabs2"] += cnt
    return jsonify(counts)


@app.route("/api/flights", methods=["GET"])
def get_flights():
    try:
        data = json.loads(FLIGHT_DATA_FILE.read_text())
        data["stale"] = (time.time() - data.get("ts", 0)) > 300
        return jsonify(data)
    except Exception:
        return jsonify({"ts": 0, "flights": [], "stale": True})


@app.route("/api/status", methods=["GET"])
def combined_status():
    """Single endpoint returning both service and display state."""
    try:
        svc = subprocess.run(
            ["systemctl", "is-active", "FlightTracker"],
            capture_output=True, text=True, timeout=5,
        )
        running = svc.stdout.strip() == "active"
    except subprocess.TimeoutExpired:
        running = False
    return jsonify({
        "running": running,
        "paused": PAUSE_FLAG.exists() if running else True,
        "night": NIGHT_FLAG.exists(),
        "apis_disabled": APIS_DISABLED_FLAG.exists(),
    })


def _db_stats(date_from: str, date_to: str, today: str | None = None) -> dict | None:
    """
    Compute aggregated stats from ft_flights.db for [date_from, date_to] (inclusive).
    If `today` is provided, also computes today-specific totals for the "Today" card.
    Returns None if the DB is unavailable or any query fails.
    """
    if not DB_FILE.exists():
        return None
    conn = None
    try:
        conn = sqlite3.connect(str(DB_FILE))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")

        top_rows = conn.execute(
            "SELECT airline, COUNT(*) cnt FROM sightings "
            "WHERE date >= ? AND date <= ? GROUP BY airline ORDER BY cnt DESC LIMIT 25",
            (date_from, date_to),
        ).fetchall()

        tail_rows = conn.execute(
            "SELECT registration, COUNT(*) cnt FROM sightings "
            "WHERE date >= ? AND date <= ? AND registration != '' "
            "GROUP BY registration ORDER BY cnt DESC LIMIT 25",
            (date_from, date_to),
        ).fetchall()

        route_rows = conn.execute(
            "SELECT origin || '→' || destination route, COUNT(*) cnt "
            "FROM sightings "
            "WHERE date >= ? AND date <= ? AND origin != '' AND destination != '' "
            "GROUP BY origin, destination ORDER BY cnt DESC LIMIT 50",
            (date_from, date_to),
        ).fetchall()

        type_rows = conn.execute(
            "SELECT aircraft, COUNT(*) cnt FROM sightings "
            "WHERE date >= ? AND date <= ? AND aircraft != '' "
            "GROUP BY aircraft ORDER BY cnt DESC LIMIT 25",
            (date_from, date_to),
        ).fetchall()

        bucket_rows = conn.execute(
            """
            SELECT
              CASE
                WHEN route_source = 'none'     THEN 'none'
                WHEN route_source = 'override'  THEN 'override'
                WHEN (route_source LIKE '%airlabs%' OR route_source LIKE '%aeroapi%')
                     AND route_source NOT LIKE '%+%'  THEN 'paid'
                WHEN (route_source LIKE '%airlabs%' OR route_source LIKE '%aeroapi%')
                     AND route_source     LIKE '%+%'  THEN 'mixed'
                ELSE 'free'
              END bucket,
              COUNT(*) cnt
            FROM sightings
            WHERE date >= ? AND date <= ? AND route_source != ''
            GROUP BY bucket
            """,
            (date_from, date_to),
        ).fetchall()

        day_rows = conn.execute(
            "SELECT date, COUNT(*) cnt FROM sightings "
            "WHERE date >= ? AND date <= ? GROUP BY date",
            (date_from, date_to),
        ).fetchall()

        ac_rows = conn.execute(
            "SELECT api_name, SUM(count) total FROM api_calls "
            "WHERE date >= ? AND date <= ? GROUP BY api_name",
            (date_from, date_to),
        ).fetchall()

        day_ac_rows = conn.execute(
            "SELECT date, api_name, count FROM api_calls "
            "WHERE date >= ? AND date <= ?",
            (date_from, date_to),
        ).fetchall()

        today_total = None
        today_top   = None
        today_ac    = None
        if today:
            today_total = conn.execute(
                "SELECT COUNT(*) FROM sightings WHERE date = ?", (today,)
            ).fetchone()[0]
            today_top = conn.execute(
                "SELECT airline, COUNT(*) cnt FROM sightings "
                "WHERE date = ? GROUP BY airline ORDER BY cnt DESC LIMIT 5",
                (today,),
            ).fetchall()
            today_ac_rows = conn.execute(
                "SELECT api_name, count FROM api_calls WHERE date = ?",
                (today,),
            ).fetchall()
            today_ac = {r["api_name"]: r["count"] for r in today_ac_rows}

    except Exception as exc:
        _log(f"[web] DB stats error: {exc}")
        return None
    finally:
        if conn:
            conn.close()

    buckets       = {r["bucket"]: r["cnt"] for r in bucket_rows}
    total_sourced = sum(buckets.values())
    source_pct    = {}
    if total_sourced:
        for b in ("free", "paid", "mixed", "none", "override"):
            v = buckets.get(b, 0)
            if v:
                source_pct[b] = round(v / total_sourced * 100, 1)

    day_counts  = {r["date"]: r["cnt"] for r in day_rows}
    range_total = sum(day_counts.values())

    day_api_calls: dict[str, dict] = {}
    for r in day_ac_rows:
        day_api_calls.setdefault(r["date"], {})[r["api_name"]] = r["count"]

    return {
        "today_total":     today_total,
        "today_top":       [
            {"prefix": r["airline"], "count": r["cnt"], "name": AIRLINE_NAMES.get(r["airline"], "")}
            for r in (today_top or [])
        ],
        "today_api_calls": today_ac or {},
        "range_total":     range_total,
        "range_top":       [
            {"prefix": r["airline"], "count": r["cnt"], "name": AIRLINE_NAMES.get(r["airline"], "")}
            for r in top_rows
        ],
        "range_api_calls": {r["api_name"]: r["total"] for r in ac_rows},
        "day_api_calls":   day_api_calls,
        "rollup": {
            "flights":    range_total,
            "airlines":   [
                {"prefix": r["airline"], "count": r["cnt"], "name": AIRLINE_NAMES.get(r["airline"], "")}
                for r in top_rows
            ],
            "tails":      [{"reg": r["registration"], "count": r["cnt"]} for r in tail_rows],
            "routes":     [{"route": r["route"],       "count": r["cnt"]} for r in route_rows],
            "types":      [{"type": r["aircraft"],     "count": r["cnt"]} for r in type_rows],
            "source_pct": source_pct,
        },
        "day_counts": day_counts,
    }


# ── Log-parsing regex (mirrors backfill_db.py) ────────────────────────────────
_LOG_ROUTE_RE = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]"
    r"\s+\[route:([^\]]*)\]"
    r"\s+\[type:[^\]]*\]"
    r"\s+([A-Z][A-Z0-9]{2,9})"
    r"(?:\s+\([^)]*\))?"
    r"\s+([A-Z?]{3})->([A-Z?]{3})"
)
_LOG_AIRCRAFT_RE = re.compile(r"'([^']*)'\s*([A-Z][A-Z0-9-]{3,})?\s*$")


def _parse_log_sightings(log_path: Path) -> list:
    """
    Parse plane.log and return sightings list (newest-first).
    Used as fallback when ft_flights.db is unavailable.
    """
    sightings = []
    try:
        with open(log_path, errors="replace") as fh:
            for raw in fh:
                line = raw.rstrip()
                if "[TEST:" in line:
                    continue
                m = _LOG_ROUTE_RE.match(line)
                if not m:
                    continue
                seen_at, route_src, callsign, origin, dest = m.groups()
                am = _LOG_AIRCRAFT_RE.search(line, m.end())
                aircraft     = am.group(1) if am else ""
                registration = (am.group(2) or "") if am else ""
                if origin == "?": origin = ""
                if dest   == "?": dest   = ""
                prefix = callsign[:3].upper() if len(callsign) >= 3 else ""
                sightings.append({
                    "seen_at":      seen_at,
                    "date":         seen_at[:10],
                    "time":         seen_at[11:16],
                    "callsign":     callsign,
                    "registration": registration,
                    "origin":       origin,
                    "destination":  dest,
                    "aircraft":     aircraft,
                    "route_source": route_src,
                    "airline":      AIRLINE_NAMES.get(prefix, ""),
                })
    except Exception:
        pass
    return list(reversed(sightings))


def _db_recent(date_from: str, date_to: str, limit: int = 50) -> list:
    """Return the most recent sightings in [date_from, date_to], newest first, up to limit."""
    if not DB_FILE.exists():
        return []
    conn = None
    try:
        conn = sqlite3.connect(str(DB_FILE))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        rows = conn.execute(
            """
            SELECT seen_at, date, callsign, registration, origin, destination,
                   aircraft, route_source, airline
            FROM   sightings
            WHERE  date >= ? AND date <= ?
            ORDER  BY seen_at DESC
            LIMIT  ?
            """,
            (date_from, date_to, limit),
        ).fetchall()
        return [
            {
                "seen_at":      r["seen_at"],
                "date":         r["date"],
                "time":         r["seen_at"][11:16] if r["seen_at"] else "",
                "callsign":     r["callsign"],
                "registration": r["registration"],
                "origin":       r["origin"],
                "destination":  r["destination"],
                "aircraft":     r["aircraft"],
                "route_source": r["route_source"],
                "airline":      AIRLINE_NAMES.get(r["airline"], ""),
            }
            for r in rows
        ]
    except Exception as exc:
        _log(f"[web] DB recent error: {exc}")
        return []
    finally:
        if conn:
            conn.close()


def _db_search(q: str, limit: int = 100, offset: int = 0) -> tuple[list, int] | None:
    """
    Search sightings in ft_flights.db (case-insensitive substring match on
    callsign, registration, origin, or destination).
    Returns (rows_newest_first, total_match_count) or None if the DB is unavailable.
    Supports pagination via limit/offset; limit is capped at 200 per page.
    total_match_count reflects the real number of matches so the UI can show
    "showing 100 of 1,450" accurately.
    """
    if not DB_FILE.exists():
        return None
    conn = None
    limit  = min(max(int(limit), 1), 200)
    offset = max(int(offset), 0)
    try:
        conn = sqlite3.connect(str(DB_FILE))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        like = f"%{q}%"
        params = (like, like, like, like)
        where  = ("callsign LIKE ? OR registration LIKE ? "
                  "OR origin LIKE ? OR destination LIKE ?")
        total = conn.execute(
            f"SELECT COUNT(*) FROM sightings WHERE {where}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT seen_at, date, callsign, registration, origin, destination,
                   aircraft, route_source, airline
            FROM sightings
            WHERE {where}
            ORDER BY seen_at DESC
            LIMIT {limit} OFFSET {offset}
            """,
            params,
        ).fetchall()
        return (
            [
                {
                    "seen_at":      r["seen_at"],
                    "date":         r["date"],
                    "time":         r["seen_at"][11:16] if r["seen_at"] else "",
                    "callsign":     r["callsign"],
                    "registration": r["registration"],
                    "origin":       r["origin"],
                    "destination":  r["destination"],
                    "aircraft":     r["aircraft"],
                    "route_source": r["route_source"],
                    "airline":      AIRLINE_NAMES.get(r["airline"], ""),
                }
                for r in rows
            ],
            total,
        )
    except Exception as exc:
        _log(f"[web] DB search error: {exc}")
        return None
    finally:
        if conn:
            conn.close()


@app.route("/api/stats", methods=["GET"])
def flight_stats():
    """
    Return flight stats.  Supports two modes:
      Default (?days=N): today card + rolling 90-day period.
      Range   (?from=YYYY-MM-DD&to=YYYY-MM-DD): stats for a custom date range.
    Source: ft_flights.db (sightings + api_calls tables).
    """
    today = datetime.now(_PACIFIC).strftime("%Y-%m-%d")
    try:
        days = min(int(request.args.get("days", 90)), 90)
    except (ValueError, TypeError):
        days = 90

    range_from = (request.args.get("from") or "").strip()
    range_to   = (request.args.get("to")   or "").strip()
    is_range   = bool(range_from and range_to)

    if is_range:
        try:
            datetime.strptime(range_from, "%Y-%m-%d")
            datetime.strptime(range_to,   "%Y-%m-%d")
            if range_from > range_to:
                range_from, range_to = range_to, range_from
        except ValueError:
            return jsonify({"error": "Invalid date — use YYYY-MM-DD"}), 400

    # ── Range mode ───────────────────────────────────────────────────────────
    if is_range:
        db = _db_stats(range_from, range_to)
        if db is None:
            return jsonify({"error": "Database unavailable"}), 503

        history  = []
        cur_date = datetime.strptime(range_from, "%Y-%m-%d")
        end_date = datetime.strptime(range_to,   "%Y-%m-%d")
        while cur_date <= end_date:
            d      = cur_date.strftime("%Y-%m-%d")
            day_ac = db["day_api_calls"].get(d, {})
            history.append({
                "date":    d,
                "total":   db["day_counts"].get(d, 0),
                "airlabs": day_ac.get("airlabs", 0),
                "aeroapi": day_ac.get("aeroapi", 0),
            })
            cur_date += timedelta(days=1)

        return jsonify({
            "mode":       "range",
            "today":      today,
            "range_from": range_from,
            "range_to":   range_to,
            "total":      db["range_total"],
            "api_calls":  {"airlabs": db["range_api_calls"].get("airlabs", 0),
                           "aeroapi": db["range_api_calls"].get("aeroapi", 0)},
            "top_today":  db["range_top"],
            "rollup":     db["rollup"],
            "history":    history,
            "recent":     _db_recent(range_from, range_to),
        })

    # ── Default mode: today card + 90-day period ─────────────────────────────
    cutoff = (datetime.now(_PACIFIC) - timedelta(days=90)).strftime("%Y-%m-%d")
    db = _db_stats(cutoff, today, today=today)
    if db is None:
        return jsonify({"error": "Database unavailable"}), 503

    history = []
    for i in range(days - 1, -1, -1):
        d      = (datetime.now(_PACIFIC) - timedelta(days=i)).strftime("%Y-%m-%d")
        day_ac = db["day_api_calls"].get(d, {})
        history.append({
            "date":    d,
            "total":   db["day_counts"].get(d, 0),
            "airlabs": day_ac.get("airlabs", 0),
            "aeroapi": day_ac.get("aeroapi", 0),
        })

    ac     = db["today_api_calls"]
    rac    = db["range_api_calls"]
    return jsonify({
        "mode":             "default",
        "today":            today,
        "range_from":       cutoff,
        "range_to":         today,
        "total":            db["today_total"],
        "api_calls":        {"airlabs": ac.get("airlabs", 0),  "aeroapi": ac.get("aeroapi", 0)},
        "range_api_calls":  {"airlabs": rac.get("airlabs", 0), "aeroapi": rac.get("aeroapi", 0)},
        "top_today":        db["today_top"],
        "rollup":           db["rollup"],
        "history":          history,
        "recent":           _db_recent(cutoff, today),
    })


@app.route("/api/stats/search", methods=["GET"])
def stats_search():
    """
    Search flight history for overhead sightings.
    Primary source: ft_flights.db (full history since DB was created).
    Fallback: parse plane.log directly (rolling window, ~days of data).
    Matches callsign, tail registration, origin, or destination (substring, case-insensitive).
    Returns up to 200 results newest-first.
    """
    q = (request.args.get("q") or "").strip().upper()
    if len(q) < 2:
        return jsonify({"error": "Query must be at least 2 characters"}), 400

    try:
        limit  = min(int(request.args.get("limit",  100)), 200)
        offset = max(int(request.args.get("offset",   0)),   0)
    except (ValueError, TypeError):
        limit, offset = 100, 0

    db_result = _db_search(q, limit=limit, offset=offset)
    source    = "db"

    if db_result is None:
        # DB unavailable — fall back to log parsing (no pagination support)
        all_sightings = _parse_log_sightings(LOG_PATH)
        sightings = [
            s for s in all_sightings
            if q in s["callsign"]
            or q in s["registration"]
            or q in s["origin"]
            or q in s["destination"]
        ]
        total_count = len(sightings)
        sightings = sightings[:limit]
        source = "log"
    else:
        sightings, total_count = db_result

    return jsonify({
        "query":     q,
        "count":     total_count,
        "offset":    offset,
        "returned":  len(sightings),
        "source":    source,
        "sightings": sightings,
    })


@app.route("/api/free-api-accuracy", methods=["GET"])
def free_api_accuracy():
    """Accuracy report: adsbdb (free) vs. paid-API route cross-checks."""
    if not DB_FILE.exists():
        return jsonify({"error": "Database unavailable"}), 503
    conn = None
    try:
        today      = datetime.now(_PACIFIC).strftime("%Y-%m-%d")
        thirty_ago = (datetime.now(_PACIFIC) - timedelta(days=29)).strftime("%Y-%m-%d")
        conn = sqlite3.connect(str(DB_FILE))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row

        def _row_stats(row):
            total     = row["total"]     or 0
            matches   = int(row["matches"]   or 0)
            mismatches= int(row["mismatches"]or 0)
            pct       = round(matches / total * 100, 1) if total else None
            return {"total": total, "matches": matches, "mismatches": mismatches, "pct": pct}

        # ── Today ──────────────────────────────────────────────────────────
        t = conn.execute(
            """SELECT COUNT(*) total, SUM(matched) matches, SUM(1-matched) mismatches
               FROM free_api_checks WHERE date=?""", (today,)
        ).fetchone()

        # ── 30-day ─────────────────────────────────────────────────────────
        t30 = conn.execute(
            """SELECT COUNT(*) total, SUM(matched) matches, SUM(1-matched) mismatches
               FROM free_api_checks WHERE date>=?""", (thirty_ago,)
        ).fetchone()

        # ── Last 10 mismatches ──────────────────────────────────────────────
        mm = conn.execute(
            """SELECT seen_at, callsign, free_route, paid_route
               FROM free_api_checks WHERE matched=0
               ORDER BY id DESC LIMIT 10"""
        ).fetchall()

        # ── Daily breakdown ─────────────────────────────────────────────────
        daily = conn.execute(
            """SELECT date, COUNT(*) total, SUM(matched) matches
               FROM free_api_checks WHERE date>=?
               GROUP BY date ORDER BY date""", (thirty_ago,)
        ).fetchall()

        return jsonify({
            "today":           _row_stats(t),
            "thirty_day":      _row_stats(t30),
            "last_mismatches": [dict(r) for r in mm],
            "daily":           [{"date": r["date"], "total": r["total"],
                                 "matches": r["matches"] or 0} for r in daily],
        })
    except Exception as e:
        _log(f"[server] free-api-accuracy error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@app.route("/api/display", methods=["GET"])
def display_status():
    try:
        svc = subprocess.run(
            ["systemctl", "is-active", "FlightTracker"],
            capture_output=True, text=True, timeout=5,
        )
        active = svc.stdout.strip() == "active"
    except subprocess.TimeoutExpired:
        active = False
    if not active:
        return jsonify({"paused": True})
    return jsonify({"paused": PAUSE_FLAG.exists()})


@app.route("/api/display/off", methods=["POST"])
def display_off():
    PAUSE_FLAG.touch()
    _log("[web] display off")
    return jsonify({"ok": True})


@app.route("/api/display/on", methods=["POST"])
def display_on():
    PAUSE_FLAG.unlink(missing_ok=True)
    _log("[web] display on")
    return jsonify({"ok": True})


@app.route("/api/service", methods=["GET"])
def service_status():
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "FlightTracker"],
            capture_output=True, text=True, timeout=5,
        )
        running = result.stdout.strip() == "active"
    except subprocess.TimeoutExpired:
        running = False
    return jsonify({"running": running})


@app.route("/api/service/stop", methods=["POST"])
def service_stop():
    try:
        subprocess.run(
            ["sudo", "/usr/bin/systemctl", "stop", "FlightTracker"],
            check=True, capture_output=True, timeout=15,
        )
        _log("[web] service stopped")
        return jsonify({"ok": True})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "systemctl timed out"}), 504
    except subprocess.CalledProcessError as e:
        return jsonify({"error": (e.stderr or b"").decode()}), 500


@app.route("/api/service/start", methods=["POST"])
def service_start():
    try:
        subprocess.run(
            ["sudo", "/usr/bin/systemctl", "start", "FlightTracker"],
            check=True, capture_output=True, timeout=15,
        )
        _log("[web] service started")
        return jsonify({"ok": True})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "systemctl timed out"}), 504
    except subprocess.CalledProcessError as e:
        return jsonify({"error": (e.stderr or b"").decode()}), 500


def _billing_period_start(reset_day):
    """Return the current billing period start as YYYY-MM-DD."""
    today = datetime.now(_PACIFIC)
    if today.day >= reset_day:
        return today.replace(day=reset_day).strftime("%Y-%m-%d")
    first_of_month = today.replace(day=1)
    last_month = first_of_month - timedelta(days=1)
    # Clamp reset_day to actual days in last month (defensive for reset_day > 28)
    actual_day = min(reset_day, calendar.monthrange(last_month.year, last_month.month)[1])
    return last_month.replace(day=actual_day).strftime("%Y-%m-%d")


def _billing_period_end(reset_day):
    """Return the last day of the current billing period as YYYY-MM-DD."""
    today = datetime.now(_PACIFIC)
    # Determine year/month of the next reset date
    if today.day >= reset_day:
        y = today.year + 1 if today.month == 12 else today.year
        m = 1 if today.month == 12 else today.month + 1
    else:
        y, m = today.year, today.month
    # Clamp reset_day to actual days in that month (defensive for reset_day > 28)
    actual_day = min(reset_day, calendar.monthrange(y, m)[1])
    next_reset = today.replace(year=y, month=m, day=actual_day)
    return (next_reset - timedelta(days=1)).strftime("%Y-%m-%d")


def _read_usage_file(path, reset_day):
    """Read a usage JSON file, resetting if the billing period has rolled over."""
    period = _billing_period_start(reset_day)
    try:
        data = json.loads(Path(path).read_text())
        if data.get("period_start") == period:
            return data
    except Exception:
        pass
    return {"period_start": period, "value": 0.0}


@app.route("/api/usage", methods=["GET"])
def api_usage():
    """Return current billing-period API usage and estimated costs."""
    try:
        _cfg = read_config()
    except Exception:
        _cfg = {}
    _al_limit      = int(_cfg.get("AIRLABS_MONTHLY_LIMIT",  AIRLABS_MONTHLY_LIMIT))
    _al_reset_day  = int(_cfg.get("AIRLABS_RESET_DAY",      AIRLABS_RESET_DAY))
    _al2_limit     = int(_cfg.get("AIRLABS2_MONTHLY_LIMIT", 1000))
    _al2_reset_day = int(_cfg.get("AIRLABS2_RESET_DAY",     9))
    _fa_reset_day  = int(_cfg.get("AEROAPI_RESET_DAY",      AEROAPI_RESET_DAY))
    _fa_credit     = float(_cfg.get("FEEDER_MONTHLY_CREDIT", FEEDER_MONTHLY_CREDIT))

    al   = _read_usage_file(AIRLABS_USAGE_FILE,  _al_reset_day)
    al2  = _read_usage_file(AIRLABS2_USAGE_FILE, _al2_reset_day)
    fa   = _read_usage_file(AEROAPI_USAGE_FILE,  _fa_reset_day)
    al_calls  = int(al.get("value", 0))
    al2_calls = int(al2.get("value", 0))
    fa_spend  = round(float(fa.get("value", 0.0)), 4)
    fa_calls  = round(fa_spend / AEROAPI_COST_PER_CALL)

    return jsonify({
        "airlabs": {
            "calls":        al_calls,
            "limit":        _al_limit,
            "remaining":    max(0, _al_limit - al_calls),
            "pct_used":     round(al_calls / _al_limit * 100, 1) if _al_limit else 0,
            "period_start": al.get("period_start"),
            "period_end":   _billing_period_end(_al_reset_day),
            "resets_day":   _al_reset_day,
        },
        "airlabs2": {
            "calls":        al2_calls,
            "limit":        _al2_limit,
            "remaining":    max(0, _al2_limit - al2_calls),
            "pct_used":     round(al2_calls / _al2_limit * 100, 1) if _al2_limit else 0,
            "period_start": al2.get("period_start"),
            "period_end":   _billing_period_end(_al2_reset_day),
            "resets_day":   _al2_reset_day,
        },
        "flightaware": {
            "calls":          int(fa_calls),
            "est_spend":      fa_spend,
            "monthly_credit": _fa_credit,
            "remaining":      round(max(0.0, _fa_credit - fa_spend), 4),
            "pct_used":       round(fa_spend / _fa_credit * 100, 1) if _fa_credit else 0,
            "period_start":   fa.get("period_start"),
            "period_end":     _billing_period_end(_fa_reset_day),
            "resets_day":     _fa_reset_day,
        },
    })


@app.route("/api/usage/adjust", methods=["POST"])
def api_usage_adjust():
    """Manually correct the tracked usage count for the current billing period.

    Payload: { "api": "airlabs"|"flightaware", "value": <number> }
      - airlabs   → value is call count (integer)
      - flightaware → value is dollar spend (float, e.g. 4.253)
    """
    data = request.json or {}
    api  = data.get("api")
    try:
        value = float(data.get("value", -1))
        if value < 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "value must be a non-negative number"}), 400
    if api not in ("airlabs", "airlabs2", "flightaware"):
        return jsonify({"error": "Unknown api; expected 'airlabs', 'airlabs2', or 'flightaware'"}), 400

    try:
        _cfg = read_config()
    except Exception:
        _cfg = {}
    _al_reset_day  = int(_cfg.get("AIRLABS_RESET_DAY",  AIRLABS_RESET_DAY))
    _al2_reset_day = int(_cfg.get("AIRLABS2_RESET_DAY", 9))
    _fa_reset_day  = int(_cfg.get("AEROAPI_RESET_DAY",  AEROAPI_RESET_DAY))

    if api == "airlabs":
        path      = AIRLABS_USAGE_FILE
        period    = _billing_period_start(_al_reset_day)
        write_val = int(round(value))          # stored as integer call count
    elif api == "airlabs2":
        path      = AIRLABS2_USAGE_FILE
        period    = _billing_period_start(_al2_reset_day)
        write_val = int(round(value))          # stored as integer call count
    else:
        path      = AEROAPI_USAGE_FILE
        period    = _billing_period_start(_fa_reset_day)
        write_val = round(value, 4)            # stored as dollar spend

    # Atomic write — temp file then rename so a crash mid-write can't corrupt the file
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps({"period_start": period, "value": write_val}, indent=2))
    tmp.replace(path)
    return jsonify({"ok": True})


@app.route("/api/apis", methods=["GET"])
def api_stack():
    """Describe the route-data API stack, their status, and estimated call volumes."""
    cfg = {}
    try:
        cfg = read_config()
    except Exception:
        pass

    _adsbdb_ttl  = int(cfg.get("ADSBDB_CACHE_TTL",     3600))
    _opensky_ttl = int(cfg.get("OPENSKY_CACHE_TTL",    3600))
    _sched_ttl   = int(cfg.get("ROUTE_TTL_SCHEDULED",  604800))
    _default_ttl = int(cfg.get("ROUTE_TTL_DEFAULT",    3600))
    _miss_ttl    = int(cfg.get("ROUTE_MISS_TTL",       300))
    _paid_miss   = int(cfg.get("ROUTE_PAID_MISS_TTL",  7200))

    def _fmt_ttl(s):
        if s >= 86400: return f"{s // 86400}d"
        if s >= 3600:  return f"{s // 3600}h"
        if s >= 60:    return f"{s // 60}m"
        return f"{s}s"

    # Estimates based on observed log data: ~5–10 unique flights/hour in zone,
    # 1-hour cache TTL, adsbdb handles most LAS-origin commercial traffic directly.
    return jsonify({
        "stack": [
            {
                "priority":     1,
                "name":         "adsbdb",
                "api_key":      "adsbdb",
                "type":         "Route data (static historical DB)",
                "url":          "https://api.adsbdb.com",
                "cost":         "Free — no key required",
                "key_set":      False,
                "requires_key": False,
                "disabled":     ADSBDB_DISABLED_FLAG.exists(),
                "cache_ttl":    _adsbdb_ttl,
                "cache_ttl_fmt": _fmt_ttl(_adsbdb_ttl),
                "notes":    "Queried for every callsign (result cached to avoid repeat calls). "
                            "Trusted for GA / non-scheduled flights when origin is a configured "
                            "local airport AND passes a plausibility check. "
                            "Commercial airline callsigns (scheduled prefix) are NOT trusted — "
                            "adsbdb's static historical DB can reflect a prior leg flown by the "
                            "same aircraft on a different day. AirLabs / AeroAPI are used instead "
                            "and their result is logged against adsbdb for visibility. "
                            f"Cache TTL: {_fmt_ttl(_adsbdb_ttl)}.",
            },
            {
                "priority":     2,
                "name":         "OpenSky",
                "api_key":      "opensky",
                "type":         "Route data (real-time, by hex — free, unlimited)",
                "url":          "https://opensky-network.org",
                "cost":         "Free with credentials — no monthly limit",
                "key_set":      bool(cfg.get("OPENSKY_CLIENT_ID")),
                "requires_key": True,
                "disabled":     OPENSKY_DISABLED_FLAG.exists(),
                "cache_ttl":    _opensky_ttl,
                "cache_ttl_fmt": _fmt_ttl(_opensky_ttl),
                "notes":    "Queried before AirLabs to conserve the monthly quota. "
                            "Result trusted when origin is a configured local airport "
                            "(LOCAL_AIRPORTS) — only departures are accepted. "
                            f"Cache TTL: {_fmt_ttl(_opensky_ttl)}.",
            },
            {
                "priority":     3,
                "name":         "AirLabs",
                "api_key":      "airlabs",
                "type":         "Route data (real-time, by callsign)",
                "url":          "https://airlabs.co",
                "cost":         f"Free — {int(cfg.get('AIRLABS_MONTHLY_LIMIT', AIRLABS_MONTHLY_LIMIT)):,} calls/month",
                "key_set":      bool(cfg.get("AIRLABS_API_KEY")),
                "requires_key": True,
                "disabled":     AIRLABS_DISABLED_FLAG.exists(),
                "cache_ttl":    _sched_ttl,
                "cache_ttl_fmt": _fmt_ttl(_sched_ttl),
                "notes":    "Handles through-traffic that OpenSky couldn't auto-trust. "
                            "Returns airport coordinates for geometry plausibility. "
                            f"Cache TTL: {_fmt_ttl(_sched_ttl)} (scheduled) / {_fmt_ttl(_default_ttl)} (GA). "
                            f"Per-callsign miss cache: {_fmt_ttl(_miss_ttl)}. "
                            "Falls back to AirLabs 2 automatically when this key's quota is exhausted.",
            },
            {
                "priority":     4,
                "name":         "AirLabs 2",
                "api_key":      "airlabs2",
                "type":         "Route data (real-time, secondary key)",
                "url":          "https://airlabs.co",
                "cost":         f"Free — {int(cfg.get('AIRLABS2_MONTHLY_LIMIT', 1000)):,} calls/month",
                "key_set":      bool(cfg.get("AIRLABS_API_KEY_2")),
                "requires_key": True,
                "disabled":     AIRLABS2_DISABLED_FLAG.exists(),
                "cache_ttl":    _sched_ttl,
                "cache_ttl_fmt": _fmt_ttl(_sched_ttl),
                "notes":    "Secondary AirLabs key — called when AirLabs 1 did not make a live "
                            "200 call (in backoff, cache hit with no data, disabled, or not configured). "
                            "Skipped if AirLabs 1 made a live call and returned empty (same backend). "
                            "Preserves AeroAPI quota when AirLabs 1 runs dry. "
                            f"Cache TTL: {_fmt_ttl(_sched_ttl)} (scheduled) / {_fmt_ttl(_default_ttl)} (GA). "
                            f"Per-callsign miss cache: {_fmt_ttl(_miss_ttl)}. "
                            f"Combined paid-API miss suppression: {_fmt_ttl(_paid_miss)}.",
            },
            {
                "priority":     5,
                "name":         "FlightAware AeroAPI",
                "api_key":      "flightaware",
                "type":         "Route data (real-time, paid)",
                "url":          "https://aeroapi.flightaware.com",
                "cost":         f"${float(cfg.get('FEEDER_MONTHLY_CREDIT', FEEDER_MONTHLY_CREDIT)):.2f}/month feeder credit — ${AEROAPI_COST_PER_CALL:.3f}/call thereafter",
                "key_set":      bool(cfg.get("FLIGHTAWARE_API_KEY")),
                "requires_key": True,
                "disabled":     AEROAPI_DISABLED_FLAG.exists(),
                "cache_ttl":    _sched_ttl,
                "cache_ttl_fmt": _fmt_ttl(_sched_ttl),
                "notes":    "True last resort — only called when all free sources and both AirLabs "
                            "keys return no route. Cascades automatically on 402 (credit exhausted) or 429. "
                            f"Cache TTL: {_fmt_ttl(_sched_ttl)} (scheduled) / {_fmt_ttl(_default_ttl)} (GA). "
                            f"Miss suppression: {_fmt_ttl(_paid_miss)}.",
            },
            {
                "priority":     6,
                "name":         "LOCAL_AIRPORTS trust filter",
                "api_key":      None,
                "type":         "Trust filter (not a data source)",
                "url":          None,
                "cost":         "Free",
                "key_set":      bool(cfg.get("LOCAL_AIRPORTS") or cfg.get("LOCAL_AIRPORT")),
                "requires_key": False,
                "disabled":     False,
                "cache_ttl":    None,
                "cache_ttl_fmt": None,
                "notes":    "Routes from adsbdb and OpenSky are only accepted when the "
                            "departure airport is one of the configured LOCAL_AIRPORTS. "
                            "Paid APIs (AirLabs, AeroAPI) use geometry plausibility instead.",
            },
            {
                "priority":     0,
                "name":         "airplanes.live",
                "api_key":      None,
                "type":         "Aircraft type lookup (not in route chain)",
                "url":          "https://api.airplanes.live",
                "cost":         "Free — no key required",
                "key_set":      False,
                "requires_key": False,
                "disabled":     False,
                "cache_ttl":    86400,
                "cache_ttl_fmt": "24h",
                "notes":    "Used for aircraft type/model (e.g. 'BOEING 737-800'). "
                            "Separate from the route chain; falls back to adsbdb aircraft endpoint. "
                            "Results cached 24 hr.",
            },
        ],
        "ttls": {
            "adsbdb_cache":       _adsbdb_ttl,
            "opensky_cache":      _opensky_ttl,
            "route_scheduled":    _sched_ttl,
            "route_default":      _default_ttl,
            "route_miss":         _miss_ttl,
            "route_paid_miss":    _paid_miss,
        },
    })


@app.route("/api/overrides", methods=["GET"])
def get_overrides():
    """Return the current override rules list from SQLite."""
    conn = None
    try:
        conn = sqlite3.connect(str(DB_FILE))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        rows = conn.execute(
            "SELECT pattern, origin, destination, display, plane, note "
            "FROM overrides ORDER BY position, id"
        ).fetchall()
        return jsonify([
            {
                "pattern":     r[0],
                "origin":      r[1],
                "destination": r[2],
                "display":     r[3],
                "plane":       r[4],
                "note":        r[5],
            }
            for r in rows
        ])
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@app.route("/api/overrides", methods=["POST"])
def save_overrides():
    """Replace the full override rules list in SQLite and bump the version counter."""
    conn = None
    try:
        data = request.json
        if not isinstance(data, list):
            return jsonify({"error": "Expected a JSON array"}), 400
        for rule in data:
            if not isinstance(rule, dict) or not rule.get("pattern", "").strip():
                return jsonify({"error": "Each rule must have a non-empty 'pattern'"}), 400
        # Normalise: uppercase pattern and airport codes, strip whitespace
        clean = [
            (
                pos,
                rule["pattern"].strip().upper(),
                rule.get("origin",      "").strip().upper(),
                rule.get("destination", "").strip().upper(),
                rule.get("display",     "").strip(),
                rule.get("plane",       "").strip(),
                rule.get("note",        "").strip(),
            )
            for pos, rule in enumerate(data)
        ]
        conn = sqlite3.connect(str(DB_FILE))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("DELETE FROM overrides")
        conn.executemany(
            "INSERT INTO overrides (position, pattern, origin, destination, display, plane, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            clean,
        )
        # Ensure version row exists, then increment it so overhead.py reloads on next poll
        conn.execute("INSERT OR IGNORE INTO overrides_meta (key, value) VALUES ('version', '0')")
        conn.execute(
            "UPDATE overrides_meta "
            "SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) "
            "WHERE key='version'"
        )
        conn.commit()
        _log(f"[web] overrides saved to DB ({len(clean)} rules)")
        return jsonify({"ok": True, "count": len(clean)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@app.route("/api/test_flight", methods=["POST"])
def test_flight():
    """
    Run a full no-cache API lookup for testing.  Same pipeline as a real
    overhead flight — override rules, adsbdb, OpenSky, AirLabs, AeroAPI —
    with two exceptions: no cache reads/writes, and the LOCAL_AIRPORTS
    heuristic is skipped.  All log lines are prefixed [TEST:{callsign}].

    The result is also written to /tmp/ft_test_display.json so the next
    grab_data() cycle injects the flight into the LED matrix for 30 s.
    """
    try:
        data = request.json or {}
        callsign = (data.get("callsign") or "").strip().upper()
        if not callsign:
            return jsonify({"error": "callsign is required"}), 400
        if len(callsign) > 20 or not callsign.replace("-", "").isalnum():
            return jsonify({"error": "invalid callsign"}), 400

        # Lazy import — overhead.py is already loaded by flight-tracker.py
        # when running as a service, so this just retrieves the cached module.
        import sys
        if str(BASE_DIR) not in sys.path:
            sys.path.insert(0, str(BASE_DIR))
        from utilities.overhead import run_test_lookup

        result = run_test_lookup(callsign, use_cache=bool(data.get("use_cache", True)))
        _log(
            f"[web] test flight: {callsign}"
            f" → {result.get('final_origin','?')}->{result.get('final_destination','?')}"
            f" [{result.get('route_source','?')}]"
            f" '{result.get('final_plane','')}' [{result.get('type_source','?')}]"
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/test_flight", methods=["DELETE"])
def clear_test_flight():
    """Remove the test display injection file (clears the 30 s display window)."""
    Path("/tmp/ft_test_display.json").unlink(missing_ok=True)
    _log("[web] test flight display cleared")
    return jsonify({"ok": True})


@app.route("/api/log/history")
def log_history():
    try:
        result = subprocess.run(
            ["tail", "-n", "500", str(LOG_PATH)], capture_output=True, text=True
        )
        if result.returncode != 0:
            return jsonify({"lines": [], "error": result.stderr or "tail failed"})
        return jsonify({"lines": result.stdout.splitlines()})
    except Exception as e:
        return jsonify({"lines": [], "error": str(e)})


@app.route("/api/log/stream")
def log_stream():
    def generate():
        yield "retry: 5000\n\n"   # tell browser to wait 5 s before auto-reconnecting
        log_inode = None
        try:
            with open(LOG_PATH) as f:
                log_inode = os.fstat(f.fileno()).st_ino
                f.seek(0, 2)
                while True:
                    line = f.readline()
                    if line:
                        yield f"data: {line.rstrip()}\n\n"
                    else:
                        # Detect log rotation (inode change)
                        try:
                            if os.stat(LOG_PATH).st_ino != log_inode:
                                break  # generator ends; EventSource client auto-reconnects
                        except OSError:
                            pass
                        time.sleep(0.3)
        except GeneratorExit:
            return
        except Exception:
            return

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True, use_reloader=False)
