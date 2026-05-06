import os
import subprocess
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request, Response, stream_with_context

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True

BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH = BASE_DIR / "config.py"
LOG_PATH = Path.home() / "plane.log"
PAUSE_FLAG = Path("/tmp/ft_paused")

# Keys written by write_config — used to round-trip unknown keys safely
_KNOWN_KEYS = {
    "ZONE_HOME", "LOCATION_HOME", "WEATHER_LOCATION", "OPENWEATHER_API_KEY",
    "TEMPERATURE_UNITS", "MIN_ALTITUDE", "MAX_ALTITUDE", "BRIGHTNESS",
    "GPIO_SLOWDOWN", "JOURNEY_CODE_SELECTED", "JOURNEY_BLANK_FILLER",
    "HAT_PWM_ENABLED", "RECEIVER_HOST", "LOCAL_AIRPORT",
    "OPENSKY_CLIENT_ID", "OPENSKY_CLIENT_SECRET", "FLIGHTAWARE_API_KEY",
}


def read_config():
    """Execute config.py in a sandboxed namespace and return its variables."""
    safe_globals = {"__builtins__": {}}
    with open(CONFIG_PATH) as f:
        exec(compile(f.read(), str(CONFIG_PATH), "exec"), safe_globals)
    return {k: v for k, v in safe_globals.items() if not k.startswith("_")}


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

    # Overlay only the known managed keys from the POST body
    for k in _KNOWN_KEYS:
        if k in data:
            existing[k] = data[k]

    zone = existing["ZONE_HOME"]
    loc  = existing["LOCATION_HOME"]

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
JOURNEY_CODE_SELECTED = {repr(str(existing["JOURNEY_CODE_SELECTED"]))}
JOURNEY_BLANK_FILLER = {repr(str(existing["JOURNEY_BLANK_FILLER"]))}
HAT_PWM_ENABLED = {bool(existing["HAT_PWM_ENABLED"])}

RECEIVER_HOST = {repr(str(existing["RECEIVER_HOST"]))}

LOCAL_AIRPORT = {repr(str(existing["LOCAL_AIRPORT"]))}
OPENSKY_CLIENT_ID = {repr(str(existing["OPENSKY_CLIENT_ID"]))}
OPENSKY_CLIENT_SECRET = {repr(str(existing["OPENSKY_CLIENT_SECRET"]))}
FLIGHTAWARE_API_KEY = {repr(str(existing["FLIGHTAWARE_API_KEY"]))}
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
            subprocess.Popen(["sudo", "systemctl", "restart", "FlightTracker"])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/display", methods=["GET"])
def display_status():
    return jsonify({"paused": PAUSE_FLAG.exists()})


@app.route("/api/display/off", methods=["POST"])
def display_off():
    PAUSE_FLAG.touch()
    return jsonify({"ok": True})


@app.route("/api/display/on", methods=["POST"])
def display_on():
    PAUSE_FLAG.unlink(missing_ok=True)
    return jsonify({"ok": True})


@app.route("/api/log/history")
def log_history():
    try:
        result = subprocess.run(
            ["tail", "-n", "500", str(LOG_PATH)], capture_output=True, text=True
        )
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
                                break  # outer loop will reopen
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
