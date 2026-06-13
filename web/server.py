import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, unquote
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template, request, Response, stream_with_context
from werkzeug.exceptions import RequestEntityTooLarge

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
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0   # always revalidate static CSS/JS (no stale assets after an update)
# Cap request bodies so a LAN client can't make waitress buffer / Flask parse an
# arbitrarily large JSON body into memory on a RAM-constrained Pi — oversized
# requests are rejected with 413 before get_json() ever parses them.
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024

# ── LAN hardening (no password by design — single-user trusted LAN) ───────────
# CSRF: a cross-site page can't set a custom request header without a CORS preflight
# (which this app never grants), so requiring one on every state-changing request blocks
# drive-by POST/DELETE from other sites the operator's browser happens to have open. The
# dashboard's own JS sets this header on every fetch.
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

@app.before_request
def _csrf_guard():
    if request.method in _MUTATING_METHODS and request.headers.get("X-Requested-With") != "FlightTracker":
        return jsonify({"error": "missing or invalid X-Requested-With header"}), 403
    # Reject oversized bodies up front with 413, before any handler reads/parses them —
    # MAX_CONTENT_LENGTH is otherwise enforced lazily and the handlers' broad excepts
    # would turn the resulting RequestEntityTooLarge into a generic 500.
    cl = request.content_length
    if cl is not None and cl > app.config["MAX_CONTENT_LENGTH"]:
        return jsonify({"error": "request body too large"}), 413

@app.errorhandler(RequestEntityTooLarge)
def _too_large(_e):
    # Backstop for chunked bodies with no Content-Length: the cap is enforced when the
    # body is read; surface a clean 413 here instead of an opaque error.
    return jsonify({"error": "request body too large"}), 413

@app.after_request
def _security_headers(resp):
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    return resp

BASE_DIR = Path(__file__).parent.parent

# Ensure the project root is on sys.path so the utility imports below work
# whether server.py is launched from its own directory or from the project root.
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
_WEB_DIR = Path(__file__).parent
if str(_WEB_DIR) not in sys.path:
    sys.path.insert(0, str(_WEB_DIR))   # so sibling modules (stats_data, …) import cleanly

from functools import partial as _partial
import stats_data
from stats_data import _db_stats, _parse_log_sightings, _db_recent, _db_search
from usage_data import _billing_period_start, _billing_period_end, _read_usage_file
import scoreboard_data
from scoreboard_data import _fetch_scoreboard_data, _load_persisted_game_ended_at, _persist_game_ended_at, _clear_persisted_game_ended_at
CONFIG_PATH      = BASE_DIR / "config.py"
PAUSE_FLAG       = Path("/tmp/ft_paused")
NIGHT_FLAG       = Path("/tmp/ft_night")
APIS_DISABLED_FLAG    = Path("/tmp/ft_apis_disabled")
ADSBDB_DISABLED_FLAG  = Path("/tmp/ft_adsbdb_disabled")
OPENSKY_DISABLED_FLAG = Path("/tmp/ft_opensky_disabled")
AIRLABS_DISABLED_FLAG  = Path("/tmp/ft_airlabs_disabled")
AIRLABS2_DISABLED_FLAG = Path("/tmp/ft_airlabs2_disabled")
AEROAPI_DISABLED_FLAG  = Path("/tmp/ft_aeroapi_disabled")
FR24_DISABLED_FLAG     = Path("/tmp/ft_fr24_disabled")

_API_FLAGS: dict[str, Path] = {
    "adsbdb":      ADSBDB_DISABLED_FLAG,
    "opensky":     OPENSKY_DISABLED_FLAG,
    "airlabs":     AIRLABS_DISABLED_FLAG,
    "airlabs2":    AIRLABS2_DISABLED_FLAG,
    "flightaware": AEROAPI_DISABLED_FLAG,
    "fr24":        FR24_DISABLED_FLAG,
}
FLIGHT_DATA_FILE = Path("/tmp/ft_data.json")

AIRLABS_USAGE_FILE  = BASE_DIR / "airlabs_usage.json"
AIRLABS2_USAGE_FILE = BASE_DIR / "airlabs2_usage.json"
AEROAPI_USAGE_FILE  = BASE_DIR / "aeroapi_usage.json"
OVERRIDES_FILE     = BASE_DIR / "ft_overrides.json"
DB_FILE            = BASE_DIR / "ft_flights.db"

# Wire the extracted stats / history data layer with the DB path + stdout logger.
stats_data.bind(DB_FILE, _log)

AIRLABS_MONTHLY_LIMIT = 1000    # free tier cap

AEROAPI_COST_PER_CALL = 0.005   # $0.005 per call
FEEDER_MONTHLY_CREDIT = 10.00   # FlightAware feeder credit
AIRLABS_RESET_DAY     = 9       # AirLabs billing period resets on the 9th
AEROAPI_RESET_DAY     = 1       # FlightAware credit resets on the 1st

# ── Config schema: the single source of truth for every key the web UI manages ──
# Each entry is (NAME, kind, default) with kind in {str,int,int0,bool,float,float0,list}, or
# (NAME, fn) where fn(existing) -> a Python-source value string for the few keys with
# custom fallbacks.  `None` emits a blank line.  write_config() generates config.py
# straight from this list and _KNOWN_KEYS is *derived* from it — so adding a key is a
# one-line change and a key can never drift out of sync between the writer and the
# known-keys set.  (The old failure mode: a key listed in _KNOWN_KEYS but forgotten in
# the hand-written f-string template was silently blanked on the next save — and some of
# those keys are API credentials.)
def _cfg_literal(existing, name, kind, default):
    """Render one managed value as a Python-source literal (matches the legacy template)."""
    v = existing.get(name)
    if kind == "str":   return repr(str(v or default))
    if kind == "int":   return str(int(v) if str(v).strip() not in ("", "None") else default)
    if kind == "int0":  return str(int(v) if str(v).strip() not in ("", "None") else default)   # 0 is meaningful
    if kind == "bool":
        v = existing.get(name, default)
        # A bool key may arrive as the string "False"/"0"/"no" — bool("False") is True,
        # so normalize string inputs before coercing (the UI sends real JSON booleans,
        # but a non-standard caller or a future <select> field could send strings).
        v = (v.strip().lower() not in ("", "false", "0", "no", "off")) if isinstance(v, str) else bool(v)
        return str(v)
    if kind == "float": return str(float(v or default))
    if kind == "float0": return str(float(v if v is not None and str(v).strip() != "" else default))  # 0.0 is meaningful
    if kind == "list":  return repr(v or default)
    raise ValueError(f"unknown config kind {kind!r} for {name}")


# Scalar keys, in the order they're written to config.py (None = a blank separator line).
_CONFIG_SCHEMA = [
    ("WEATHER_LOCATION",                    "str",   ""),
    ("OPENWEATHER_API_KEY",                 "str",   ""),
    ("TEMPERATURE_UNITS",                   "str",   "imperial"),
    ("MIN_ALTITUDE",                        "int0",  100),  # 0 = no altitude floor (valid)
    ("MAX_ALTITUDE",                        "int",   15000),
    ("BRIGHTNESS",                          "int0",  80),   # 0 = panel dark (valid)
    ("GPIO_SLOWDOWN",                       "int0",  2),
    ("NIGHT_BRIGHTNESS",                    "int0",  20),   # 0 = display off at night (valid)
    ("JOURNEY_CODE_SELECTED",               "str",   ""),
    ("JOURNEY_BLANK_FILLER",                "str",   " ? "),
    ("DATE_FORMAT",                         "str",   "MDY"),
    ("HAT_PWM_ENABLED",                     "bool",  True),
    None,
    ("RECEIVER_HOST",                       "str",   "localhost"),
    ("RECEIVER_TYPE",                       "str",   "dump1090"),
    ("POLL_INTERVAL",                       "int",   15),
    ("DATA_CHECK_INTERVAL",                 "int",   2),
    None,
    ("LOCAL_AIRPORTS", lambda e: repr(str(e.get("LOCAL_AIRPORTS") or e.get("LOCAL_AIRPORT") or ""))),
    ("OPENSKY_CLIENT_ID",                   "str",   ""),
    ("OPENSKY_CLIENT_SECRET",               "str",   ""),
    ("FLIGHTAWARE_API_KEY",                 "str",   ""),
    ("AIRLABS_API_KEY",                     "str",   ""),
    ("AIRLABS_API_KEY_2",                   "str",   ""),
    ("TIMEZONE",                            "str",   "America/Los_Angeles"),
    ("LOADING_LED_ENABLED",                 "bool",  False),
    ("LOADING_LED_GPIO_PIN",                "int",   25),
    ("RAINFALL_ENABLED",                    "bool",  False),
    ("SCOREBOARD_ENABLED",                  "bool",  False),
    ("SCOREBOARD_POST_GAME_MINUTES",        "int0",  30),  # 0 = hide immediately (valid)
    ("SCOREBOARD_GOAL_CELEBRATION_SECONDS", "int0",  30),  # 0 = disable celebration (valid)
    ("SCOREBOARD_PRIORITY",                 "list",  ["NHL", "NFL", "MLB", "NBA", "MLS"]),
    ("SCOREBOARD_NHL_ENABLED",  lambda e: str(bool(e.get("SCOREBOARD_NHL_ENABLED", e.get("SCOREBOARD_ENABLED", True))))),
    ("SCOREBOARD_NHL_TEAM_ID",  lambda e: str(int(e.get("SCOREBOARD_NHL_TEAM_ID") or e.get("SCOREBOARD_TEAM_ID") or 0))),
    ("SCOREBOARD_NHL_TEAM_NAME", lambda e: repr(str(e.get("SCOREBOARD_NHL_TEAM_NAME") or e.get("SCOREBOARD_TEAM_NAME") or ""))),
    ("SCOREBOARD_NFL_ENABLED",              "bool",  False),
    ("SCOREBOARD_NFL_TEAM_ID",              "int",   0),
    ("SCOREBOARD_NFL_TEAM_NAME",            "str",   ""),
    ("SCOREBOARD_MLB_ENABLED",              "bool",  False),
    ("SCOREBOARD_MLB_TEAM_ID",              "int",   0),
    ("SCOREBOARD_MLB_TEAM_NAME",            "str",   ""),
    ("SCOREBOARD_NBA_ENABLED",              "bool",  False),
    ("SCOREBOARD_NBA_TEAM_ID",              "int",   0),
    ("SCOREBOARD_NBA_TEAM_NAME",            "str",   ""),
    ("SCOREBOARD_MLS_ENABLED",              "bool",  False),
    ("SCOREBOARD_MLS_TEAM_ID",              "int",   0),
    ("SCOREBOARD_MLS_TEAM_NAME",            "str",   ""),
    ("FEEDER_MONTHLY_CREDIT",               "float0", 10.00),  # 0.0 = no feeder credit (valid)
    ("AIRLABS_MONTHLY_LIMIT",               "int",   1000),
    ("AIRLABS_RESET_DAY",                   "int",   9),
    ("AIRLABS2_MONTHLY_LIMIT",              "int",   1000),
    ("AIRLABS2_RESET_DAY",                  "int",   9),
    ("AEROAPI_RESET_DAY",                   "int",   1),
    ("ADSBDB_CACHE_TTL",                    "int",   3600),
    ("OPENSKY_CACHE_TTL",                   "int",   3600),
    ("ROUTE_TTL_SCHEDULED",                 "int",   604800),
    ("ROUTE_TTL_DEFAULT",                   "int",   3600),
    ("ROUTE_MISS_TTL",                      "int",   300),
    ("ROUTE_PAID_MISS_TTL",                 "int",   7200),
]

# Required structured keys, written as a header block ahead of the scalar schema.
_STRUCTURED_KEYS = ("ZONE_HOME", "LOCATION_HOME")
# Legacy keys: accepted into _KNOWN_KEYS so they're dropped (migrated out) on the next
# save rather than re-emitted as "extra" user keys.
_LEGACY_KEYS = {"LOCAL_AIRPORT", "SCOREBOARD_LEAGUE", "SCOREBOARD_TEAM_ID", "SCOREBOARD_TEAM_NAME"}

# Every key the web UI manages — derived from the schema, so it can never disagree
# with what write_config() actually emits.
_KNOWN_KEYS = (set(_STRUCTURED_KEYS)
               | {e[0] for e in _CONFIG_SCHEMA if e is not None}
               | _LEGACY_KEYS)

# Secret/key fields that must never be overwritten with an empty string.
# The UI populates these from the loaded config, but if the page loads in a
# degraded state (e.g. config read failed) the field is blank — saving it would
# wipe the real key stored on disk.
_SENSITIVE_KEYS = {
    "OPENWEATHER_API_KEY", "OPENSKY_CLIENT_SECRET",
    "FLIGHTAWARE_API_KEY", "AIRLABS_API_KEY", "AIRLABS_API_KEY_2",
}

# Placeholder sent to the browser in place of real secrets (GET /api/config). Saving it
# back unchanged is treated as "keep the existing key" — secrets never leave the Pi.
_SECRET_SENTINEL = "********"


_config_cache = {"mtime": None, "data": None}
# Guards the read_config() cache miss/refill so the data/mtime pair is published
# atomically — otherwise two waitress workers can both miss and exec config.py at
# once, racing the two assignments (harmless, but this avoids the duplicate exec).
_config_cache_lock = threading.Lock()

# Serializes the read-overlay-write in write_config() so two concurrent POST /api/config
# requests (waitress runs threads=8) can't race on the temp file / clobber each other's
# content. Paired with a unique temp name per write so the loser can't 500 on os.replace.
_config_write_lock = threading.Lock()

def read_config():
    """Execute config.py in a sandboxed namespace and return its variables.
    Cached by file mtime so the timer-polled endpoints don't re-exec the file every hit."""
    try:
        mtime = os.path.getmtime(CONFIG_PATH)
    except OSError:
        mtime = None
    if _config_cache["data"] is None or _config_cache["mtime"] != mtime:
        with _config_cache_lock:
            # Re-check inside the lock: another thread may have just refilled it.
            if _config_cache["data"] is None or _config_cache["mtime"] != mtime:
                safe_globals = {"__builtins__": {}}
                with open(CONFIG_PATH) as f:
                    exec(compile(f.read(), str(CONFIG_PATH), "exec"), safe_globals)
                _config_cache["data"] = {k: v for k, v in safe_globals.items() if not k.startswith("_")}
                _config_cache["mtime"] = mtime
    return dict(_config_cache["data"])   # shallow copy so callers (and masking) can't mutate the cache


# Update timezone from config — _log() looks up _PACIFIC at call time so this applies immediately
try:
    _PACIFIC = ZoneInfo(read_config().get("TIMEZONE", "America/Los_Angeles"))
except Exception:
    pass

# Inject read_config into the extracted scoreboard data layer (team preferences).
scoreboard_data.bind(read_config)


def write_config(data):
    """
    Write config.py. Preserves any keys not managed by the web UI
    by reading the current config first and overlaying the new values.

    Serialized under _config_write_lock so concurrent saves (waitress threads=8)
    can't interleave the read-overlay-write or race on the temp file.
    """
    with _config_write_lock:
        _write_config_unlocked(data)


def _write_config_unlocked(data):
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
            if k in _SENSITIVE_KEYS and (not data[k] or data[k] == _SECRET_SENTINEL) and existing.get(k):
                continue  # empty or the masked sentinel = unchanged → keep the existing value
            existing[k] = data[k]

    # Validate TIMEZONE before writing — an invalid string would break the service on restart
    tz_str = str(existing.get("TIMEZONE", "America/Los_Angeles"))
    try:
        ZoneInfo(tz_str)
    except Exception:
        existing["TIMEZONE"] = "America/Los_Angeles"

    # Clamp DATE_FORMAT to known-good values
    if existing.get("DATE_FORMAT") not in ("MDY", "DMY", "YMD"):
        existing["DATE_FORMAT"] = "MDY"
    if existing.get("RECEIVER_TYPE") not in ("dump1090", "vrs"):
        existing["RECEIVER_TYPE"] = "dump1090"

    zone = existing.get("ZONE_HOME") or data.get("ZONE_HOME")
    loc  = existing.get("LOCATION_HOME") or data.get("LOCATION_HOME")
    if not zone or not loc:
        raise ValueError("ZONE_HOME and LOCATION_HOME are required")

    lines = [
        "ZONE_HOME = {",
        f'    "tl_y": {float(zone["tl_y"])},',
        f'    "tl_x": {float(zone["tl_x"])},',
        f'    "br_y": {float(zone["br_y"])},',
        f'    "br_x": {float(zone["br_x"])}',
        "}",
        "LOCATION_HOME = [",
        f"    {float(loc[0])},",
        f"    {float(loc[1])},",
        f"    {float(loc[2])}",
        "]",
    ]
    # Every scalar key, generated straight from _CONFIG_SCHEMA (one source of truth with
    # _KNOWN_KEYS) — no hand-maintained template, so a key can't be silently dropped.
    for _entry in _CONFIG_SCHEMA:
        if _entry is None:
            lines.append("")
        elif callable(_entry[1]):
            lines.append(f"{_entry[0]} = {_entry[1](existing)}")
        else:
            _name, _kind, _default = _entry
            lines.append(f"{_name} = {_cfg_literal(existing, _name, _kind, _default)}")
    content = "\n".join(lines) + "\n"

    # Preserve any extra keys not in the schema (e.g. custom user keys) verbatim.
    for k, v in existing.items():
        if k not in _KNOWN_KEYS and not k.startswith("_"):
            content += f"{k} = {repr(v)}\n"

    # Atomic write: write to a unique temp file then rename so a crash mid-write
    # can't leave config.py truncated or empty. A unique temp name (not a fixed
    # ".tmp") means two concurrent writers can't clobber each other's temp or hit
    # FileNotFoundError on replace — see _config_write_lock below.
    fd, tmp = tempfile.mkstemp(dir=str(CONFIG_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, CONFIG_PATH)
    except BaseException:
        # Replace failed (or write raised) — don't leave an orphaned temp behind.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/config", methods=["GET"])
def get_config():
    try:
        cfg = read_config()
        # Never send real secrets to the browser — mask them. write_config() treats the
        # sentinel (or empty) as "unchanged", so the masked value round-trips safely on save.
        for _k in _SENSITIVE_KEYS:
            if cfg.get(_k):
                cfg[_k] = _SECRET_SENTINEL
        return jsonify(cfg)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/config/reveal", methods=["POST"])
def reveal_secret():
    # Return the real value of ONE sensitive field, on explicit user request (the eye button).
    # POST (not GET) so it falls under _csrf_guard and the key/secret never land in a URL —
    # keeping them out of access logs and browser history.
    # Secrets are masked in GET /api/config by default; this is the opt-in, one-key-at-a-time
    # reveal — never auto-loaded into the page.
    body = request.get_json(silent=True) or {}
    key = body.get("key", "")
    if key not in _SENSITIVE_KEYS:
        return jsonify({"error": "not a revealable field"}), 400
    try:
        return jsonify({"value": read_config().get(key, "")})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


_restart_lock = threading.Lock()

def _trigger_restart():
    """Restart the display service once, debounced — rapid saves can't spawn racing restarts."""
    def _do_restart():
        if not _restart_lock.acquire(blocking=False):
            return  # a restart is already in flight
        try:
            subprocess.run(
                ["sudo", "/usr/bin/systemctl", "restart", "FlightTracker"],
                check=True, capture_output=True, timeout=15,
            )
            _log("[web] service restarted via config save")
        except Exception as e:
            _log(f"[web] ERROR: service restart failed: {e}")
        finally:
            _restart_lock.release()
    threading.Thread(target=_do_restart, daemon=True).start()


@app.route("/api/config", methods=["POST"])
def post_config():
    try:
        data = request.get_json(force=True, silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Invalid payload"}), 400
        write_config(data)
        if data.get("restart"):
            _trigger_restart()
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
    data = request.get_json(force=True, silent=True) or {}
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

    if api:
        return jsonify({"ok": False, "error": f"unknown api: {api}"}), 400

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
    data = request.get_json(force=True, silent=True) or {}
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
        "fr24":        "DELETE FROM cache WHERE cache_type='route' AND "
                       "(key LIKE 'fr24:%' OR source='fr24')",
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
    data  = request.get_json(force=True, silent=True) or {}
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
        for key in [value, f"airlabs:{value}", f"airlabs2:{value}", f"fr24:{value}"]:
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
    conn = None
    try:
        conn = sqlite3.connect(str(DB_FILE))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT cache_type, source, COUNT(*) cnt FROM cache WHERE expires_at>? GROUP BY cache_type, source",
            (now,)
        ).fetchall()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()

    counts = {"adsbdb": 0, "opensky": 0, "airlabs": 0, "airlabs2": 0, "flightaware": 0, "fr24": 0, "resolved": 0}
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
            if "fr24"     in parts: counts["fr24"]     += cnt
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
    except (subprocess.TimeoutExpired, OSError):
        running = False
    return jsonify({
        "running": running,
        "paused": PAUSE_FLAG.exists() if running else True,
        "night": NIGHT_FLAG.exists(),
        "apis_disabled": APIS_DISABLED_FLAG.exists(),
    })


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
                "airlabs": day_ac.get("airlabs", 0) + day_ac.get("airlabs2", 0),
                "aeroapi": day_ac.get("aeroapi", 0),
            })
            cur_date += timedelta(days=1)

        return jsonify({
            "mode":       "range",
            "today":      today,
            "range_from": range_from,
            "range_to":   range_to,
            "total":      db["range_total"],
            "api_calls":  {"airlabs": db["range_api_calls"].get("airlabs", 0) + db["range_api_calls"].get("airlabs2", 0),
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
            "airlabs": day_ac.get("airlabs", 0) + day_ac.get("airlabs2", 0),
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
        "api_calls":        {"airlabs": ac.get("airlabs", 0) + ac.get("airlabs2", 0),  "aeroapi": ac.get("aeroapi", 0)},
        "range_api_calls":  {"airlabs": rac.get("airlabs", 0) + rac.get("airlabs2", 0), "aeroapi": rac.get("aeroapi", 0)},
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
        sightings = sightings[offset:offset + limit]
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


@app.route("/api/ga-accuracy", methods=["GET"])
def ga_accuracy():
    """Accuracy report: adsbdb / OpenSky (free) vs. FR24 route cross-checks for GA (N-number) aircraft."""
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

        # Silently return empty data if the table doesn't exist yet
        # (e.g. running an old DB before the migration has run).
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if "ga_free_api_checks" not in tables:
            return jsonify({
                "adsbdb":  {"today": {"total": 0}, "thirty_day": {"total": 0}, "last_mismatches": [], "daily": []},
                "opensky": {"today": {"total": 0}, "thirty_day": {"total": 0}, "last_mismatches": [], "daily": []},
            })

        def _row_stats(row):
            total      = row["total"]      or 0
            matches    = int(row["matches"]    or 0)
            mismatches = int(row["mismatches"] or 0)
            pct        = round(matches / total * 100, 1) if total else None
            return {"total": total, "matches": matches, "mismatches": mismatches, "pct": pct}

        def _api_stats(api_name):
            t = conn.execute(
                """SELECT COUNT(*) total, SUM(matched) matches, SUM(1-matched) mismatches
                   FROM ga_free_api_checks WHERE date=? AND free_api=?""",
                (today, api_name),
            ).fetchone()
            t30 = conn.execute(
                """SELECT COUNT(*) total, SUM(matched) matches, SUM(1-matched) mismatches
                   FROM ga_free_api_checks WHERE date>=? AND free_api=?""",
                (thirty_ago, api_name),
            ).fetchone()
            mm = conn.execute(
                """SELECT seen_at, registration, callsign, free_route, fr24_route
                   FROM ga_free_api_checks WHERE matched=0 AND free_api=?
                   ORDER BY id DESC LIMIT 10""",
                (api_name,),
            ).fetchall()
            daily = conn.execute(
                """SELECT date, COUNT(*) total, SUM(matched) matches
                   FROM ga_free_api_checks WHERE date>=? AND free_api=?
                   GROUP BY date ORDER BY date""",
                (thirty_ago, api_name),
            ).fetchall()
            return {
                "today":           _row_stats(t),
                "thirty_day":      _row_stats(t30),
                "last_mismatches": [dict(r) for r in mm],
                "daily":           [{"date": r["date"], "total": r["total"],
                                     "matches": r["matches"] or 0} for r in daily],
            }

        return jsonify({
            "adsbdb":  _api_stats("adsbdb"),
            "opensky": _api_stats("opensky"),
        })
    except Exception as e:
        _log(f"[server] ga-accuracy error: {e}")
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
    except (subprocess.TimeoutExpired, OSError):
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


# ── Weather endpoint ──────────────────────────────────────────────────────────
_weather_cache: dict = {"temp": None, "unit": "°F", "ts": 0.0}
_weather_cache_lock  = threading.Lock()
# In-flight guard: only one thread refreshes a cold/expired cache; concurrent
# requests serve the (stale) cached value instead of each firing a duplicate 5 s
# upstream call (wasted OpenWeather quota + worker threads blocked in parallel).
_weather_fetch_lock  = threading.Lock()
_WEATHER_CACHE_TTL   = 60  # seconds


@app.route("/api/weather", methods=["GET"])
def api_weather():
    """Return current temperature, cached for 60 s. Uses OpenWeather if a key
    is configured, otherwise falls back to taps-aff (metric only)."""
    now = time.time()
    with _weather_cache_lock:
        cached = _weather_cache.copy()
    # Serve ANY entry within the TTL, including a None temp from a failed fetch (negative
    # cache): otherwise a weather outage makes every request re-run the up-to-~10s upstream
    # calls and pin a waitress worker. None just renders blank for the TTL; the next miss retries.
    if now - cached["ts"] < _WEATHER_CACHE_TTL:
        return jsonify({"temp": cached["temp"], "unit": cached["unit"]})

    # Dedup the cold-cache stampede: if another thread is already fetching, don't
    # issue a second upstream call — return whatever is cached (possibly stale).
    if not _weather_fetch_lock.acquire(blocking=False):
        return jsonify({"temp": cached["temp"], "unit": cached["unit"]})
    try:
        # Re-check under the fetch lock — a refresh may have just completed.
        with _weather_cache_lock:
            cached = _weather_cache.copy()
        if now - cached["ts"] < _WEATHER_CACHE_TTL:
            return jsonify({"temp": cached["temp"], "unit": cached["unit"]})
        return _refresh_weather(now)
    finally:
        _weather_fetch_lock.release()


def _refresh_weather(now):
    try:
        cfg = read_config()
    except Exception:
        cfg = {}

    location = cfg.get("WEATHER_LOCATION", "").strip()
    api_key  = cfg.get("OPENWEATHER_API_KEY", "").strip()
    units    = cfg.get("TEMPERATURE_UNITS", "imperial")
    unit_sym = "°F" if units == "imperial" else "°C"

    temp = None
    if location:
        if api_key:
            try:
                # URL-encode the location at use-time so a literal space/&/= in the
                # configured value can't inject query params (keep commas — OpenWeather
                # wants city,state,country).
                url = (
                    "https://api.openweathermap.org/data/2.5/weather"
                    f"?q={quote(unquote(location), safe=',')}&appid={api_key}&units={units}"
                )
                raw  = urllib.request.urlopen(urllib.request.Request(url), timeout=5).read()
                data = json.loads(raw)
                temp = round(data["main"]["temp"])
            except Exception:
                pass
        if temp is None:
            # Fallback: taps-aff (returns °C; we convert if needed)
            try:
                url = f"https://taps-aff.co.uk/api/{quote(unquote(location), safe=',')}"
                raw  = urllib.request.urlopen(urllib.request.Request(url), timeout=5).read()
                data = json.loads(raw)
                c    = float(data["temp_c"])
                temp = round(c * 9 / 5 + 32 if units == "imperial" else c)
            except Exception:
                pass

    with _weather_cache_lock:
        _weather_cache.update({"temp": temp, "unit": unit_sym, "ts": now})
    return jsonify({"temp": temp, "unit": unit_sym})


# In-process scoreboard cache so rapid page refreshes don't hammer the NHL API.
# game_ended_at: unix timestamp when FINAL/OFF was first seen this session (None = not yet final).
# Persists in the server process so page refreshes don't reset the post-game countdown.
_scoreboard_cache      = {"game": None, "team_name": "VGK", "sport_key": "NHL", "enabled": True, "ts": 0.0, "game_ended_at": None}
_scoreboard_cache_lock = threading.Lock()
# In-flight guard: only one thread refreshes the cold/expired scoreboard cache;
# concurrent requests serve the (stale) cached value rather than each issuing a
# duplicate _fetch_scoreboard_data() upstream call.
_scoreboard_fetch_lock = threading.Lock()
_SCOREBOARD_CACHE_TTL  = 30   # seconds


def _scoreboard_response(cached):
    return jsonify({
        "game":          cached["game"],
        "team_name":     cached["team_name"],
        "sport_key":     cached.get("sport_key", "NHL"),
        "enabled":       cached["enabled"],
        "game_ended_at": cached.get("game_ended_at"),
    })


@app.route("/api/scoreboard", methods=["GET"])
def api_scoreboard():
    """Return today's game state for the configured scoreboard team (cached 30 s)."""
    now = time.time()
    with _scoreboard_cache_lock:
        cached = _scoreboard_cache.copy()
    if now - cached["ts"] < _SCOREBOARD_CACHE_TTL:
        return _scoreboard_response(cached)

    # Dedup the cold-cache stampede: if another thread is already refreshing, serve
    # the (possibly stale) cached value instead of firing a second upstream fetch.
    if not _scoreboard_fetch_lock.acquire(blocking=False):
        return _scoreboard_response(cached)
    try:
        # Re-check under the fetch lock — a refresh may have just completed.
        with _scoreboard_cache_lock:
            cached = _scoreboard_cache.copy()
        if now - cached["ts"] < _SCOREBOARD_CACHE_TTL:
            return _scoreboard_response(cached)
        return _refresh_scoreboard(now)
    finally:
        _scoreboard_fetch_lock.release()


def _refresh_scoreboard(now):
    try:
        game, team_name, sport_key, enabled = _fetch_scoreboard_data()
    except Exception:
        game, team_name, sport_key, enabled = None, "VGK", "NHL", True

    # Track when the game first became FINAL/OFF so the post-game countdown
    # survives page refreshes AND web-service restarts.
    # File I/O (read/persist) is done outside the lock to avoid blocking other
    # threads; the lock is then held for a single atomic read-compute-write.
    state   = game["state"] if game else None
    game_id = game.get("game_id") if game else None

    # Determine game_ended_at before acquiring the lock so file I/O is cheap.
    if state in ("FINAL", "OFF") and game_id:
        # Peek at the cached value without the lock (worst case: two threads
        # both compute the same value on the very first post-game poll).
        prev_ended_at_peek = _scoreboard_cache.get("game_ended_at")
        if prev_ended_at_peek is None:
            # Try the persisted file first (survives service restart).
            persisted = _load_persisted_game_ended_at(game_id)
            if persisted is None:
                # Estimate from game start rather than using now, so a service
                # restart doesn't reset the 30-min window for an already-over game.
                # Use a period-aware buffer — err on the LATER side so we never
                # hide the score before the real post-game window expires.
                #   regulation (period ≤ 3): start + 3 h  (~2.5 h typical)
                #   OT / SO   (period  > 3): start + 4 h  (conservative for multi-OT)
                try:
                    start_utc = game.get("start_time_utc", "")
                    if start_utc:
                        start_ts = datetime.fromisoformat(
                            start_utc.replace("Z", "+00:00")
                        ).timestamp()
                        period = game.get("period", 0) or 0
                        buffer = 4 * 3600 if period > 3 else 3 * 3600
                        persisted = start_ts + buffer
                    else:
                        persisted = now
                except Exception:
                    persisted = now
                _persist_game_ended_at(game_id, persisted)
            new_ended_at = persisted
        else:
            new_ended_at = prev_ended_at_peek
    else:
        new_ended_at = None  # live / no game — reset the timer
        _clear_persisted_game_ended_at()

    with _scoreboard_cache_lock:
        # If another thread already set game_ended_at, keep the earlier value.
        existing = _scoreboard_cache.get("game_ended_at")
        if new_ended_at is not None and existing is not None:
            game_ended_at = min(existing, new_ended_at)
        else:
            game_ended_at = new_ended_at  # None or first-set value
        _scoreboard_cache.update({
            "game": game, "team_name": team_name, "sport_key": sport_key,
            "enabled": enabled, "ts": now, "game_ended_at": game_ended_at,
        })
    return jsonify({
        "game":         game,
        "team_name":    team_name,
        "sport_key":    sport_key,    # e.g. "NHL", "NFL", "MLB", "NBA", "MLS"
        "enabled":      enabled,
        "game_ended_at": game_ended_at,   # unix timestamp or null
    })


@app.route("/api/service", methods=["GET"])
def service_status():
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "FlightTracker"],
            capture_output=True, text=True, timeout=5,
        )
        running = result.stdout.strip() == "active"
    except (subprocess.TimeoutExpired, OSError):
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
    except OSError as e:
        return jsonify({"error": str(e)}), 503


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
    except OSError as e:
        return jsonify({"error": str(e)}), 503


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
    data = request.get_json(force=True, silent=True) or {}
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

    # Atomic write — unique temp file then rename so a crash mid-write can't corrupt the
    # file, and two concurrent adjusts can't clobber a shared ".tmp" / 500 on replace.
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(str(path)), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps({"period_start": period, "value": write_val}, indent=2))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
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
                "name":         "AirLabs 1",
                "api_key":      "airlabs",
                "type":         "Route data (real-time, by callsign — primary key)",
                "url":          "https://airlabs.co",
                "cost":         f"Free — {int(cfg.get('AIRLABS_MONTHLY_LIMIT', AIRLABS_MONTHLY_LIMIT)):,} calls/month",
                "key_set":      bool(cfg.get("AIRLABS_API_KEY")),
                "requires_key": True,
                "disabled":     AIRLABS_DISABLED_FLAG.exists(),
                "cache_ttl":    _sched_ttl,
                "cache_ttl_fmt": _fmt_ttl(_sched_ttl),
                "notes":    "Primary AirLabs key. Called first for commercial routes. "
                            "Returns airport coordinates for geometry plausibility. "
                            "Non-local routes cached at miss TTL (5 min) — never persisted long-term. "
                            f"Local route cache TTL: {_fmt_ttl(_default_ttl)}. "
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
                "notes":    "Called when all free sources and both AirLabs keys return no route. "
                            "Falls back to FR24 (§5) if still no result. "
                            "Cascades automatically on 402 (credit exhausted) or 429. "
                            f"Cache TTL: {_fmt_ttl(_sched_ttl)} (scheduled) / {_fmt_ttl(_default_ttl)} (GA). "
                            f"Miss suppression: {_fmt_ttl(_paid_miss)}.",
            },
            {
                "priority":     6,
                "name":         "FlightRadar24",
                "api_key":      "fr24",
                "type":         "Route data (real-time, free — unofficial API)",
                "url":          "https://www.flightradar24.com",
                "cost":         "Free — no key required",
                "key_set":      False,
                "requires_key": False,
                "disabled":     FR24_DISABLED_FLAG.exists(),
                "cache_ttl":    _default_ttl,
                "cache_ttl_fmt": _fmt_ttl(_default_ttl),
                "notes":    "Serves three roles: §1 first resort for GA (N-number) aircraft — "
                            "real-time registration lookup, more reliable than adsbdb's static DB. "
                            "§5 last resort for commercial flights — called only when all paid APIs "
                            "return no route. Step 5 of the aircraft type lookup chain — queries by "
                            "registration when all static databases miss. "
                            "Not affected by the APIs On/Off toggle (free). "
                            f"Route cache TTL: {_fmt_ttl(_sched_ttl)} commercial / "
                            f"{_fmt_ttl(_default_ttl)} GA / {_fmt_ttl(_miss_ttl)} miss.",
            },
            {
                "priority":     0,
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
                            "Step 1 and 4 of a 5-step type lookup chain: "
                            "airplanes.live (by hex) → adsbdb → OpenSky metadata → "
                            "airplanes.live (by reg) → FR24. Results cached 24 hr.",
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
        data = request.get_json(force=True, silent=True)
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
    overhead flight — override rules, adsbdb, OpenSky, AirLabs, AeroAPI,
    including the same LOCAL_AIRPORTS trust filter.  The only difference is
    cache handling (no-cache mode hits every API fresh).  All log lines are
    prefixed [TEST:{callsign}].

    The result is also written to /tmp/ft_test_display.json so the next
    grab_data() cycle injects the flight into the LED matrix for 30 s.
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
        callsign = (data.get("callsign") or "").strip().upper()
        if not callsign:
            return jsonify({"error": "callsign is required"}), 400
        if len(callsign) > 20 or not callsign.replace("-", "").isalnum():
            return jsonify({"error": "invalid callsign"}), 400

        # Lazy import — overhead.py is already loaded by flight-tracker.py
        # when running as a service, so this just retrieves the cached module.
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
            ["tail", "-n", "500", str(LOG_PATH)], capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return jsonify({"lines": [], "error": result.stderr or "tail failed"})
        return jsonify({"lines": result.stdout.splitlines()})
    except Exception as e:
        return jsonify({"lines": [], "error": str(e)})


# Each open SSE stream pins one waitress worker for its whole lifetime (waitress is a
# synchronous WSGI server — threads=8). Bound how many can be open at once so a few
# stale/backgrounded log tabs can't starve every other endpoint, and cap each stream's
# lifetime so half-open connections deterministically release their thread (the client
# auto-reconnects via the 'retry: 5000' below).
_MAX_LOG_STREAMS   = 3
_LOG_STREAM_SLOTS  = threading.BoundedSemaphore(_MAX_LOG_STREAMS)
_LOG_STREAM_MAX_SECONDS = 600   # 10 min, then return; EventSource reconnects on its own


@app.route("/api/log/stream")
def log_stream():
    if not _LOG_STREAM_SLOTS.acquire(blocking=False):
        return jsonify({"error": "too many log streams open"}), 503

    def generate():
        yield "retry: 5000\n\n"   # tell browser to wait 5 s before auto-reconnecting
        log_inode = None
        _deadline = time.time() + _LOG_STREAM_MAX_SECONDS
        try:
            with open(LOG_PATH) as f:
                log_inode = os.fstat(f.fileno()).st_ino
                f.seek(0, 2)
                _last_beat = time.time()
                while True:
                    line = f.readline()
                    if line:
                        yield f"data: {line.rstrip()}\n\n"
                    else:
                        # Bound the stream's lifetime so a backgrounded/half-open tab
                        # releases its worker thread; the client auto-reconnects.
                        if time.time() >= _deadline:
                            break
                        # Heartbeat through quiet periods keeps the SSE channel alive (and
                        # makes waitress / any proxy flush); also detect log rotation.
                        if time.time() - _last_beat > 10:
                            _last_beat = time.time()
                            yield ": keepalive\n\n"
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
        finally:
            _LOG_STREAM_SLOTS.release()   # always give the worker slot back

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    _host, _port = "0.0.0.0", 5000
    try:
        from waitress import serve
        _log("[web] serving via waitress (production WSGI, bounded thread pool)")
        serve(app, host=_host, port=_port, threads=8, channel_timeout=120)
    except ImportError:
        _log("[web] waitress not installed — falling back to the Flask dev server")
        app.run(host=_host, port=_port, debug=False, threaded=True, use_reloader=False)
