import os
import subprocess
import sys
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request, Response, stream_with_context

app = Flask(__name__)

BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH = BASE_DIR / "config.py"
LOG_PATH = Path.home() / "plane.log"


def read_config():
    config = {}
    with open(CONFIG_PATH) as f:
        exec(compile(f.read(), str(CONFIG_PATH), "exec"), config)
    return {k: v for k, v in config.items() if not k.startswith("_")}


def write_config(data):
    zone = data["ZONE_HOME"]
    loc = data["LOCATION_HOME"]
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
WEATHER_LOCATION = {repr(str(data["WEATHER_LOCATION"]))}
OPENWEATHER_API_KEY = {repr(str(data["OPENWEATHER_API_KEY"]))}
TEMPERATURE_UNITS = {repr(str(data["TEMPERATURE_UNITS"]))}
MIN_ALTITUDE = {int(data["MIN_ALTITUDE"])}
MAX_ALTITUDE = {int(data["MAX_ALTITUDE"])}
BRIGHTNESS = {int(data["BRIGHTNESS"])}
GPIO_SLOWDOWN = {int(data["GPIO_SLOWDOWN"])}
JOURNEY_CODE_SELECTED = {repr(str(data["JOURNEY_CODE_SELECTED"]))}
JOURNEY_BLANK_FILLER = {repr(str(data["JOURNEY_BLANK_FILLER"]))}
HAT_PWM_ENABLED = {bool(data["HAT_PWM_ENABLED"])}

RECEIVER_HOST = {repr(str(data["RECEIVER_HOST"]))}

LOCAL_AIRPORT = {repr(str(data["LOCAL_AIRPORT"]))}
OPENSKY_CLIENT_ID = {repr(str(data["OPENSKY_CLIENT_ID"]))}
OPENSKY_CLIENT_SECRET = {repr(str(data["OPENSKY_CLIENT_SECRET"]))}
FLIGHTAWARE_API_KEY = {repr(str(data["FLIGHTAWARE_API_KEY"]))}
"""
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
        write_config(data)
        if data.get("restart"):
            subprocess.Popen(["sudo", "systemctl", "restart", "FlightTracker"])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
        try:
            with open(LOG_PATH) as f:
                f.seek(0, 2)
                while True:
                    line = f.readline()
                    if line:
                        yield f"data: {line.rstrip()}\n\n"
                    else:
                        time.sleep(0.3)
        except GeneratorExit:
            pass

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
