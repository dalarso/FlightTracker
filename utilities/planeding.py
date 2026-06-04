"""
Plane-overhead DING — fire-and-forget LAN nudge to a desktop listener.

Sibling of the scoreboard goal-horn (scenes/sportscore.py): when PLANE_DING_HOST is
set in config.py (e.g. "192.168.1.30"), the display sends a small UDP packet the instant
a NEW aircraft is put on the matrix.  A tiny listener on that machine plays a "ding"
the user picks and mirrors what's overhead.

Wire format (pipe-delimited):
  • "DING|callsign|origin|dest|type|count"   — a new plane just went up (play the ding).
  • "STATE|callsign|origin|dest|type|count"  — heartbeat (~5 s): what's currently overhead.
  • "STATE|NONE"                             — heartbeat: clear skies.

UDP is connectionless — nothing is held open; the heartbeat is how the listener knows the
Pi is alive.  Every send is non-blocking and fully swallowed: if the desktop is off, closed,
or unreachable the packet simply vanishes and the display is never affected.  Empty
PLANE_DING_HOST = feature off (every call below is a no-op).
"""

import socket
import time


def _cfg(name, default):
    try:
        import config
        return getattr(config, name, default)
    except (ImportError, AttributeError):
        return default


PLANE_DING_HOST      = str(_cfg("PLANE_DING_HOST", "")).strip()
PLANE_DING_PORT      = int(_cfg("PLANE_DING_PORT", 50506))
PLANE_DING_PING_SECS = int(_cfg("PLANE_DING_PING_SECS", 5))   # heartbeat → "connected"

_sock = None
if PLANE_DING_HOST:
    try:
        _sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _sock.setblocking(False)
    except Exception:
        _sock = None

_last_ping = 0.0


def _clean(s) -> str:
    """Make a field safe for the pipe-delimited wire format."""
    return str(s if s is not None else "").replace("|", "/").replace("\n", " ").strip()


def _fields(flight):
    cs  = _clean(flight.get("callsign")) or "N/A"
    org = _clean(flight.get("origin"))
    dst = _clean(flight.get("destination"))
    typ = _clean(flight.get("display_name") or flight.get("plane"))
    return cs, org, dst, typ


def _send(msg: str) -> None:
    if _sock is None:
        return
    try:
        _sock.sendto(msg.encode("utf-8", "replace"), (PLANE_DING_HOST, PLANE_DING_PORT))
    except Exception:
        pass


def send_ding(flight, count: int = 1) -> None:
    """Fire the instant a NEW plane is put on the matrix.  Wire: DING|cs|org|dst|type|count."""
    if _sock is None or not flight:
        return
    cs, org, dst, typ = _fields(flight)
    _send("DING|{}|{}|{}|{}|{}".format(cs, org, dst, typ, count))


def send_state(flights, now_ts: float) -> None:
    """Throttled heartbeat → connection status + what's currently overhead.
    Reports the first plane on screen (others cycle); wire STATE|cs|org|dst|type|count,
    or STATE|NONE when nothing is up.  Throttled to PLANE_DING_PING_SECS."""
    global _last_ping
    if _sock is None or now_ts - _last_ping < PLANE_DING_PING_SECS:
        return
    _last_ping = now_ts
    if flights:
        cs, org, dst, typ = _fields(flights[0])
        _send("STATE|{}|{}|{}|{}|{}".format(cs, org, dst, typ, len(flights)))
    else:
        _send("STATE|NONE")
