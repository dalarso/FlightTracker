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
APIS_DISABLED_FLAG = Path("/tmp/ft_apis_disabled")
FLIGHT_DATA_FILE = Path("/tmp/ft_data.json")

AIRLABS_USAGE_FILE = BASE_DIR / "airlabs_usage.json"
AEROAPI_USAGE_FILE = BASE_DIR / "aeroapi_usage.json"
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
    "JOURNEY_BLANK_FILLER", "HAT_PWM_ENABLED", "RECEIVER_HOST", "LOCAL_AIRPORT",
    "OPENSKY_CLIENT_ID", "OPENSKY_CLIENT_SECRET", "FLIGHTAWARE_API_KEY",
    "AIRLABS_API_KEY",
}

# Secret/key fields that must never be overwritten with an empty string.
# The UI populates these from the loaded config, but if the page loads in a
# degraded state (e.g. config read failed) the field is blank — saving it would
# wipe the real key stored on disk.
_SENSITIVE_KEYS = {
    "OPENWEATHER_API_KEY", "OPENSKY_CLIENT_SECRET",
    "FLIGHTAWARE_API_KEY", "AIRLABS_API_KEY",
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
    # Load existing config so unknown keys aren't lost
    try:
        existing = read_config()
    except Exception:
        existing = {}

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
HAT_PWM_ENABLED = {bool(existing.get("HAT_PWM_ENABLED", True))}

RECEIVER_HOST = {repr(str(existing.get("RECEIVER_HOST", "localhost")))}

LOCAL_AIRPORT = {repr(str(existing.get("LOCAL_AIRPORT", "")))}
OPENSKY_CLIENT_ID = {repr(str(existing.get("OPENSKY_CLIENT_ID", "")))}
OPENSKY_CLIENT_SECRET = {repr(str(existing.get("OPENSKY_CLIENT_SECRET", "")))}
FLIGHTAWARE_API_KEY = {repr(str(existing.get("FLIGHTAWARE_API_KEY", "")))}
AIRLABS_API_KEY = {repr(str(existing.get("AIRLABS_API_KEY", "")))}
TIMEZONE = {repr(str(existing.get("TIMEZONE", "America/Los_Angeles")))}
"""
    # Preserve any extra keys (e.g. LOADING_LED_ENABLED) not in the template
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
    """Toggle the combined kill-switch for all limited/paid APIs (AirLabs + FlightAware)."""
    if APIS_DISABLED_FLAG.exists():
        APIS_DISABLED_FLAG.unlink()
        _log("[web] limited APIs enabled")
    else:
        APIS_DISABLED_FLAG.touch()
        _log("[web] limited APIs disabled")
    return jsonify({"ok": True, "apis_disabled": APIS_DISABLED_FLAG.exists()})


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


def _db_search(q: str) -> tuple[list, int] | None:
    """
    Search sightings in ft_flights.db (case-insensitive substring match on
    callsign, registration, origin, or destination).
    Returns (rows_newest_first, total_match_count) or None if the DB is unavailable.
    Rows are capped at 200; total_match_count reflects the real number of matches
    so the UI can show "showing newest 200 of 450" accurately.
    """
    if not DB_FILE.exists():
        return None
    conn = None
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
            LIMIT 200
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

    db_result = _db_search(q)
    source    = "db"

    if db_result is None:
        # DB unavailable — fall back to log parsing
        all_sightings = _parse_log_sightings(LOG_PATH)
        sightings = [
            s for s in all_sightings
            if q in s["callsign"]
            or q in s["registration"]
            or q in s["origin"]
            or q in s["destination"]
        ]
        total_count = len(sightings)
        sightings = sightings[:200]
        source = "log"
    else:
        sightings, total_count = db_result

    return jsonify({
        "query":     q,
        "count":     total_count,
        "returned":  len(sightings),
        "source":    source,
        "sightings": sightings,
    })


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
    today = datetime.now()
    if today.day >= reset_day:
        return today.replace(day=reset_day).strftime("%Y-%m-%d")
    first_of_month = today.replace(day=1)
    last_month = first_of_month - timedelta(days=1)
    # Clamp reset_day to actual days in last month (defensive for reset_day > 28)
    actual_day = min(reset_day, calendar.monthrange(last_month.year, last_month.month)[1])
    return last_month.replace(day=actual_day).strftime("%Y-%m-%d")


def _billing_period_end(reset_day):
    """Return the last day of the current billing period as YYYY-MM-DD."""
    today = datetime.now()
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
    al  = _read_usage_file(AIRLABS_USAGE_FILE, AIRLABS_RESET_DAY)
    fa  = _read_usage_file(AEROAPI_USAGE_FILE, AEROAPI_RESET_DAY)
    al_calls  = int(al.get("value", 0))
    fa_spend  = round(float(fa.get("value", 0.0)), 4)
    fa_calls  = round(fa_spend / AEROAPI_COST_PER_CALL)

    return jsonify({
        "airlabs": {
            "calls":        al_calls,
            "limit":        AIRLABS_MONTHLY_LIMIT,
            "remaining":    max(0, AIRLABS_MONTHLY_LIMIT - al_calls),
            "pct_used":     round(al_calls / AIRLABS_MONTHLY_LIMIT * 100, 1),
            "period_start": al.get("period_start"),
            "period_end":   _billing_period_end(AIRLABS_RESET_DAY),
            "resets_day":   AIRLABS_RESET_DAY,
        },
        "flightaware": {
            "calls":          int(fa_calls),
            "est_spend":      fa_spend,
            "monthly_credit": FEEDER_MONTHLY_CREDIT,
            "remaining":      round(max(0.0, FEEDER_MONTHLY_CREDIT - fa_spend), 4),
            "pct_used":       round(fa_spend / FEEDER_MONTHLY_CREDIT * 100, 1),
            "period_start":   fa.get("period_start"),
            "period_end":     _billing_period_end(AEROAPI_RESET_DAY),
            "resets_day":     AEROAPI_RESET_DAY,
        },
    })


@app.route("/api/apis", methods=["GET"])
def api_stack():
    """Describe the route-data API stack, their status, and estimated call volumes."""
    cfg = {}
    try:
        cfg = read_config()
    except Exception:
        pass

    # Estimates based on observed log data: ~5–10 unique flights/hour in zone,
    # 1-hour cache TTL, adsbdb handles most LAS-origin commercial traffic directly.
    return jsonify({
        "stack": [
            {
                "priority":    1,
                "name":        "adsbdb",
                "type":        "Route data (static historical DB)",
                "url":         "https://api.adsbdb.com",
                "cost":        "Free — no key required",
                "key_set":     False,
                "requires_key": False,
                "notes":    "Queried for every new callsign. Result trusted immediately "
                            "when origin is LAS or VGT AND passes a geographic plausibility "
                            "check. Handles the majority of LAS-departure commercial traffic. "
                            "Est. ~20–40 live calls/day (cache TTL 1 hr).",
            },
            {
                "priority": 2,
                "name":     "OpenSky",
                "type":     "Route data (real-time, by hex — free, unlimited)",
                "url":      "https://opensky-network.org",
                "cost":        "Free with credentials — no monthly limit",
                "key_set":     bool(cfg.get("OPENSKY_CLIENT_ID")),
                "requires_key": True,
                "notes":    "Queried before AirLabs to conserve the monthly quota. "
                            "Trusted without coordinate verification when a local airport "
                            "(LAS/VGT) is confirmed by the aircraft's vertical rate: "
                            "climbing + local origin, or descending + local dest. "
                            "Covers ~90% of traffic (LAS/VGT departures & arrivals). "
                            "Through-traffic with non-local endpoints falls through to "
                            "AirLabs. Returns no airport coordinates, so the geometry "
                            "plausibility check is replaced by the vertical-rate rule. "
                            "Est. ~20–40 live calls/day.",
            },
            {
                "priority": 3,
                "name":     "AirLabs",
                "type":     "Route data (real-time, by callsign)",
                "url":      "https://airlabs.co",
                "cost":        "Free — 1,000 calls/month",
                "key_set":     bool(cfg.get("AIRLABS_API_KEY")),
                "requires_key": True,
                "notes":    "Now mainly handles through-traffic that OpenSky couldn't "
                            "auto-trust. Returns airport coordinates, enabling the full "
                            "geometry plausibility check. Requires an ICAO callsign "
                            "(e.g. SWA1137) — won't match tail-number registrations "
                            "(e.g. N911WY). Est. ~2–5 live calls/day; ~60–150/month.",
            },
            {
                "priority": 4,
                "name":     "FlightAware AeroAPI",
                "type":     "Route data (real-time, paid)",
                "url":      "https://aeroapi.flightaware.com",
                "cost":        f"${FEEDER_MONTHLY_CREDIT:.2f}/month feeder credit — ${AEROAPI_COST_PER_CALL:.3f}/call thereafter",
                "key_set":     bool(cfg.get("FLIGHTAWARE_API_KEY")),
                "requires_key": True,
                "notes":    "True last resort — only called when all free sources return "
                            "no route. Cascades automatically on 402 (credit exhausted) "
                            "or 429. With healthy upstream sources, expect <5 calls/day.",
            },
            {
                "priority": 5,
                "name":     "LOCAL_AIRPORT heuristic",
                "type":     "Heuristic fallback",
                "url":      None,
                "cost":        "Free",
                "key_set":     bool(cfg.get("LOCAL_AIRPORT")),
                "requires_key": True,
                "notes":    "Fills one missing endpoint using vertical speed when all "
                            "APIs have returned nothing: climbing → LOCAL_AIRPORT is "
                            "origin; descending → LOCAL_AIRPORT is destination.",
            },
            {
                "priority": 0,
                "name":     "airplanes.live",
                "type":     "Aircraft type lookup (not in route chain)",
                "url":      "https://api.airplanes.live",
                "cost":        "Free — no key required",
                "key_set":     False,
                "requires_key": False,
                "notes":    "Used for aircraft type/model (e.g. 'BOEING 737-800'). "
                            "Separate from the route chain; falls back to adsbdb "
                            "aircraft endpoint if needed. Results cached 24 hr. "
                            "Est. ~20–40 live calls/day.",
            },
        ]
    })


@app.route("/api/overrides", methods=["GET"])
def get_overrides():
    """Return the current override rules list."""
    try:
        return jsonify(json.loads(OVERRIDES_FILE.read_text()))
    except FileNotFoundError:
        return jsonify([])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/overrides", methods=["POST"])
def save_overrides():
    """Replace the full override rules list."""
    try:
        data = request.json
        if not isinstance(data, list):
            return jsonify({"error": "Expected a JSON array"}), 400
        for rule in data:
            if not isinstance(rule, dict) or not rule.get("pattern", "").strip():
                return jsonify({"error": "Each rule must have a non-empty 'pattern'"}), 400
        # Normalise: uppercase pattern, strip whitespace from airport codes
        clean = []
        for rule in data:
            clean.append({
                "pattern":     rule["pattern"].strip().upper(),
                "origin":      rule.get("origin", "").strip().upper(),
                "destination": rule.get("destination", "").strip().upper(),
                "plane":       rule.get("plane", "").strip(),
                "note":        rule.get("note", "").strip(),
            })
        tmp = str(OVERRIDES_FILE) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(clean, f, indent=2)
        os.replace(tmp, str(OVERRIDES_FILE))
        _log(f"[web] overrides saved ({len(clean)} rules)")
        return jsonify({"ok": True, "count": len(clean)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/test_flight", methods=["POST"])
def test_flight():
    """
    Run a full no-cache API lookup for testing.  Same pipeline as a real
    overhead flight — override rules, adsbdb, OpenSky, AirLabs, AeroAPI —
    with two exceptions: no cache reads/writes, and the LOCAL_AIRPORT
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
