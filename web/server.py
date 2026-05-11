import json
import os
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template, request, Response, stream_with_context

_PACIFIC = ZoneInfo("America/Los_Angeles")

LOG_PATH = Path.home() / "plane.log"


def _log(msg):
    ts = datetime.now(_PACIFIC).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line)
    except Exception:
        pass


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

AIRLABS_MONTHLY_LIMIT = 1000    # free tier cap
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
WEATHER_LOCATION = {repr(str(existing["WEATHER_LOCATION"]))}
OPENWEATHER_API_KEY = {repr(str(existing["OPENWEATHER_API_KEY"]))}
TEMPERATURE_UNITS = {repr(str(existing["TEMPERATURE_UNITS"]))}
MIN_ALTITUDE = {int(existing["MIN_ALTITUDE"])}
MAX_ALTITUDE = {int(existing["MAX_ALTITUDE"])}
BRIGHTNESS = {int(existing["BRIGHTNESS"])}
GPIO_SLOWDOWN = {int(existing["GPIO_SLOWDOWN"])}
NIGHT_BRIGHTNESS = {int(existing.get("NIGHT_BRIGHTNESS", 20))}
JOURNEY_CODE_SELECTED = {repr(str(existing["JOURNEY_CODE_SELECTED"]))}
JOURNEY_BLANK_FILLER = {repr(str(existing["JOURNEY_BLANK_FILLER"]))}
HAT_PWM_ENABLED = {bool(existing["HAT_PWM_ENABLED"])}

RECEIVER_HOST = {repr(str(existing["RECEIVER_HOST"]))}

LOCAL_AIRPORT = {repr(str(existing["LOCAL_AIRPORT"]))}
OPENSKY_CLIENT_ID = {repr(str(existing["OPENSKY_CLIENT_ID"]))}
OPENSKY_CLIENT_SECRET = {repr(str(existing["OPENSKY_CLIENT_SECRET"]))}
FLIGHTAWARE_API_KEY = {repr(str(existing["FLIGHTAWARE_API_KEY"]))}
AIRLABS_API_KEY = {repr(str(existing.get("AIRLABS_API_KEY", "")))}
TIMEZONE = {repr(str(existing.get("TIMEZONE", "America/Los_Angeles")))}
"""
    # Preserve any extra keys (e.g. LOADING_LED_ENABLED) not in the template
    for k, v in existing.items():
        if k not in _KNOWN_KEYS and not k.startswith("_"):
            content += f"{k} = {repr(v)}\n"

    with open(CONFIG_PATH, "w") as f:
        f.write(content)


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
    svc = subprocess.run(
        ["systemctl", "is-active", "FlightTracker"],
        capture_output=True, text=True
    )
    running = svc.stdout.strip() == "active"
    return jsonify({
        "running": running,
        "paused": PAUSE_FLAG.exists() if running else True,
        "night": NIGHT_FLAG.exists(),
        "apis_disabled": APIS_DISABLED_FLAG.exists(),
    })


@app.route("/api/display", methods=["GET"])
def display_status():
    svc = subprocess.run(
        ["systemctl", "is-active", "FlightTracker"],
        capture_output=True, text=True
    )
    if svc.stdout.strip() != "active":
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
    result = subprocess.run(
        ["systemctl", "is-active", "FlightTracker"],
        capture_output=True, text=True
    )
    return jsonify({"running": result.stdout.strip() == "active"})


@app.route("/api/service/stop", methods=["POST"])
def service_stop():
    try:
        subprocess.run(
            ["sudo", "/usr/bin/systemctl", "stop", "FlightTracker"],
            check=True, capture_output=True
        )
        _log("[web] service stopped")
        return jsonify({"ok": True})
    except subprocess.CalledProcessError as e:
        return jsonify({"error": (e.stderr or b"").decode()}), 500


@app.route("/api/service/start", methods=["POST"])
def service_start():
    try:
        subprocess.run(
            ["sudo", "/usr/bin/systemctl", "start", "FlightTracker"],
            check=True, capture_output=True
        )
        _log("[web] service started")
        return jsonify({"ok": True})
    except subprocess.CalledProcessError as e:
        return jsonify({"error": (e.stderr or b"").decode()}), 500


def _billing_period_start(reset_day):
    """Return the current billing period start as YYYY-MM-DD."""
    today = datetime.now()
    if today.day >= reset_day:
        return today.replace(day=reset_day).strftime("%Y-%m-%d")
    first_of_month = today.replace(day=1)
    last_month = first_of_month - timedelta(days=1)
    return last_month.replace(day=reset_day).strftime("%Y-%m-%d")


def _billing_period_end(reset_day):
    """Return the last day of the current billing period as YYYY-MM-DD."""
    today = datetime.now()
    # Next reset = same day next month (or this month if we haven't hit it yet)
    if today.day >= reset_day:
        # Next reset is next month on reset_day
        if today.month == 12:
            next_reset = today.replace(year=today.year + 1, month=1, day=reset_day)
        else:
            next_reset = today.replace(month=today.month + 1, day=reset_day)
    else:
        next_reset = today.replace(day=reset_day)
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
                "priority": 1,
                "name":     "adsbdb",
                "type":     "Route data (static historical DB)",
                "url":      "https://api.adsbdb.com",
                "cost":     "Free — no key required",
                "key_set":  False,
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
                "cost":     "Free with credentials — no monthly limit",
                "key_set":  bool(cfg.get("OPENSKY_CLIENT_ID")),
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
                "cost":     "Free — 1,000 calls/month",
                "key_set":  bool(cfg.get("AIRLABS_API_KEY")),
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
                "cost":     f"${FEEDER_MONTHLY_CREDIT:.2f}/month feeder credit — ${AEROAPI_COST_PER_CALL:.3f}/call thereafter",
                "key_set":  bool(cfg.get("FLIGHTAWARE_API_KEY")),
                "notes":    "True last resort — only called when all free sources return "
                            "no route. Cascades automatically on 402 (credit exhausted) "
                            "or 429. With healthy upstream sources, expect <5 calls/day.",
            },
            {
                "priority": 5,
                "name":     "LOCAL_AIRPORT heuristic",
                "type":     "Heuristic fallback",
                "url":      None,
                "cost":     "Free",
                "key_set":  bool(cfg.get("LOCAL_AIRPORT")),
                "notes":    "Fills one missing endpoint using vertical speed when all "
                            "APIs have returned nothing: climbing → LOCAL_AIRPORT is "
                            "origin; descending → LOCAL_AIRPORT is destination.",
            },
            {
                "priority": 0,
                "name":     "airplanes.live",
                "type":     "Aircraft type lookup (not in route chain)",
                "url":      "https://api.airplanes.live",
                "cost":     "Free — no key required",
                "key_set":  False,
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
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
