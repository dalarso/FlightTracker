"""Stats / history data layer for the dashboard — extracted from server.py.

Pure SQLite + log-parsing helpers behind the /api/stats, /api/stats/search and
recent-sightings endpoints.  server.py owns the DB path and the stdout logger and
injects them via bind() once they exist; the four helpers are then re-imported so
the route handlers call them unchanged.

AIRLINE_NAMES and the two log-parse regexes moved here with the helpers — nothing
outside this module references them.

Performance model (2026-11): days before today never change (the display process only
inserts and enriches rows on their own date), so the aggregates for finished days are
computed once with SQLite's GROUP BY and cached; each request only re-reads today's rows
and merges them in.  On the Pi Zero 2 W that took the 90-day stats from ~0.75 s to tens
of milliseconds.  (Pulling the rows into Python to aggregate there was *slower* on the Pi:
creating ~16k row tuples alone costs ~0.37 s.)
"""
import heapq
import re
import sqlite3
import threading
import time
from collections import Counter, OrderedDict
from datetime import datetime, timedelta
from pathlib import Path

# ── injected by server.bind() ──────────────────────────────────────────────────
DB_FILE = None        # Path to ft_flights.db (server owns it; routes use it too)


def _log(_msg):       # replaced by server's stdout logger in bind()
    pass


def bind(db_file, log):
    """Inject the DB path + logger from server.py once they exist."""
    global DB_FILE, _log
    DB_FILE, _log = db_file, log


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
    "EJA": "NetJets",              "LXJ": "Flexjet",             "JRE": "flyExclusive",
    "TIV": "Thrive Aviation",      "CXK": "ATP Flight School",
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


# ── Stats aggregation ─────────────────────────────────────────────────────────
_SIGHTING_COLS = ("seen_at, date, callsign, registration, origin, destination, "
                  "aircraft, route_source, airline")


def _sighting_dict(r) -> dict:
    """One sightings row (a tuple in _SIGHTING_COLS order) as the API's JSON shape."""
    seen_at = r[0] or ""
    return {
        "seen_at":      seen_at,
        "date":         r[1],
        "time":         seen_at[11:16],
        "callsign":     r[2],
        "registration": r[3],
        "origin":       r[4],
        "destination":  r[5],
        "aircraft":     r[6],
        "route_source": r[7],
        "airline":      AIRLINE_NAMES.get(r[8] or "", ""),
    }


# The APIs a displayed route can come from, most trusted first.  A label naming several
# (an old merge such as 'opensky+aeroapi') is credited to the most trusted one.
_ROUTE_APIS = ("fr24", "airlabs", "aeroapi", "opensky", "adsbdb", "vrs")
SOURCE_BUCKETS = _ROUTE_APIS + ("override", "none", "other")


def _route_bucket(src: str) -> str:
    """Which API supplied a displayed route.  Labels are lower-cased for matching
    ('resolved:airlabs2:cached' -> 'airlabs': a 7-day cache hit is credited to the API
    that resolved it).  'override' and 'none' are their own buckets."""
    s = (src or "").lower()
    if s in ("", "none"):
        return "none"
    if s == "override":
        return "override"
    for api in _ROUTE_APIS:
        if api in s:
            return api
    return "other"


def _top(counter, n: int) -> list:
    """The n largest counts, ties broken by key so the order is stable between calls."""
    return heapq.nsmallest(n, counter.items(), key=lambda kv: (-kv[1], kv[0]))


def _airline_rows(items) -> list:
    return [{"prefix": k, "count": c, "name": AIRLINE_NAMES.get(k, "")} for k, c in items]


def _connect():
    conn = sqlite3.connect(str(DB_FILE))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _shift(day: str, n: int) -> str:
    return (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=n)).strftime("%Y-%m-%d")


def _empty_agg() -> dict:
    return {"airlines": Counter(), "tails": {}, "routes": Counter(), "types": Counter(),
            "sources": Counter(), "days": {}, "hourly": [0] * 24}


def _agg_range(conn, date_from: str, date_to: str) -> dict:
    """Full (untruncated) aggregates for [date_from, date_to], grouped by SQLite.
    `tails` maps registration -> (count, max airline, max aircraft), like the old SQL."""
    where, params = "date >= ? AND date <= ?", (date_from, date_to)

    def run(sql):
        return conn.execute(sql.format(w=where), params).fetchall()

    agg = _empty_agg()
    for airline, c in run("SELECT airline, COUNT(*) FROM sightings WHERE {w} GROUP BY airline"):
        agg["airlines"][airline or ""] += c
    for reg, c, airline, ac in run(
            "SELECT registration, COUNT(*), MAX(airline), MAX(aircraft) FROM sightings "
            "WHERE {w} AND registration != '' GROUP BY registration"):
        agg["tails"][reg] = (c, airline or "", ac or "")
    for orig, dest, c in run(
            "SELECT origin, destination, COUNT(*) FROM sightings "
            "WHERE {w} AND origin != '' AND destination != '' GROUP BY origin, destination"):
        agg["routes"][(orig, dest)] += c
    for ac, c in run("SELECT aircraft, COUNT(*) FROM sightings WHERE {w} AND aircraft != '' GROUP BY aircraft"):
        agg["types"][ac] += c
    for src, c in run("SELECT route_source, COUNT(*) FROM sightings WHERE {w} AND route_source != '' GROUP BY route_source"):
        agg["sources"][_route_bucket(src)] += c
    agg["days"] = dict(run("SELECT date, COUNT(*) FROM sightings WHERE {w} GROUP BY date"))
    for hour, c in run("SELECT substr(seen_at, 12, 2), COUNT(*) FROM sightings WHERE {w} GROUP BY 1"):
        if hour and hour.isdigit() and int(hour) < 24:
            agg["hourly"][int(hour)] += c
    return agg


def _merge(*aggs) -> dict:
    """Combine aggregates of disjoint date spans.  A single part is returned as-is (the
    callers only read it)."""
    parts = [a for a in aggs if a]
    if not parts:
        return _empty_agg()
    if len(parts) == 1:
        return parts[0]
    out = _empty_agg()
    for a in parts:
        for k in ("airlines", "routes", "types", "sources"):
            out[k].update(a[k])
        out["days"].update(a["days"])
        out["hourly"] = [x + y for x, y in zip(out["hourly"], a["hourly"])]
        tails = out["tails"]
        for reg, (c, airline, ac) in a["tails"].items():
            t = tails.get(reg)
            tails[reg] = (c, airline, ac) if t is None else (t[0] + c, max(t[1], airline), max(t[2], ac))
    return out


# Finished-day aggregates: (db path, date_from, date_to) -> (computed_at, agg).  The TTL only
# bounds how long a manual backfill into past days could go unseen.
_PAST_TTL = 3600
_PAST_MAX = 8
_past_cache: "OrderedDict[tuple, tuple]" = OrderedDict()
_past_lock = threading.Lock()


def _past_aggregate(date_from: str, date_to: str) -> dict:
    key = (str(DB_FILE), date_from, date_to)
    with _past_lock:   # held while computing: concurrent first requests share one result
        hit = _past_cache.get(key)
        if hit and time.time() - hit[0] < _PAST_TTL:
            _past_cache.move_to_end(key)
            return hit[1]
        conn = _connect()
        try:
            agg = _agg_range(conn, date_from, date_to)
        finally:
            conn.close()
        _past_cache[key] = (time.time(), agg)
        while len(_past_cache) > _PAST_MAX:
            _past_cache.popitem(last=False)
        return agg


def warm(date_from: str, date_to: str) -> None:
    """Pre-compute a finished-day span (the server warms the default 90 days at start-up,
    so the first page load doesn't pay for it).  Never raises."""
    try:
        if DB_FILE is not None and DB_FILE.exists() and date_from <= date_to:
            _past_aggregate(date_from, date_to)
    except Exception as exc:
        _log(f"[web] stats warm-up skipped: {exc}")


def _db_stats(date_from: str, date_to: str, today: str | None = None,
              live_date: str | None = None) -> dict | None:
    """
    Aggregated stats from ft_flights.db for [date_from, date_to] (inclusive), plus the 50
    newest sightings and hour-of-day counts.  With `today`, also the "Today" card numbers
    (count, top airlines, distinct tails, hourly) and the times of yesterday's sightings,
    so the page can compare today against "this time yesterday".

    `live_date` (or `today`) is the date still being written; days before it come from the
    finished-day cache.  Without either, the whole range is read fresh.
    Returns None if the DB is unavailable or any query fails.
    """
    if not DB_FILE.exists():
        return None
    live = live_date or today
    conn = None
    try:
        conn = _connect()
        cur = None
        if live:
            past_to = min(date_to, _shift(live, -1))
            past = _past_aggregate(date_from, past_to) if date_from <= past_to else None
            cur = _agg_range(conn, live, live) if date_from <= live <= date_to else None
            agg = _merge(past, cur)
        else:
            agg = _agg_range(conn, date_from, date_to)
        t_agg = y_times = None
        if today:
            t_agg = cur if (cur is not None and today == live) else _agg_range(conn, today, today)
            y_times = [r[0] for r in conn.execute(
                "SELECT substr(seen_at, 12, 8) FROM sightings WHERE date = ? ORDER BY 1",
                (_shift(today, -1),))]
        # seen_at bounds (not date) so the idx_seen_at index serves both the range and the
        # order: no sort of the whole span to keep 50 rows.
        recent = conn.execute(
            f"SELECT {_SIGHTING_COLS} FROM sightings WHERE seen_at >= ? AND seen_at < ? "
            "ORDER BY seen_at DESC LIMIT 50",
            (date_from, _shift(date_to, 1)),
        ).fetchall()
        ac_rows = conn.execute(
            "SELECT api_name, SUM(count) FROM api_calls WHERE date >= ? AND date <= ? GROUP BY api_name",
            (date_from, date_to),
        ).fetchall()
        day_ac_rows = conn.execute(
            "SELECT date, api_name, count FROM api_calls WHERE date >= ? AND date <= ?",
            (date_from, date_to),
        ).fetchall()
        today_ac_rows = conn.execute(
            "SELECT api_name, SUM(count) FROM api_calls WHERE date = ? GROUP BY api_name", (today,),
        ).fetchall() if today else []
    except Exception as exc:
        _log(f"[web] DB stats error: {exc}")
        return None
    finally:
        if conn:
            conn.close()

    sources = agg["sources"]
    total_sourced = sum(sources.values())
    source_pct = {}
    if total_sourced:
        for b in SOURCE_BUCKETS:
            if sources.get(b):
                source_pct[b] = round(sources[b] / total_sourced * 100, 1)

    day_api_calls: dict[str, dict] = {}
    for date, api, count in day_ac_rows:
        day_api_calls.setdefault(date, {})[api] = count

    days = agg["days"]
    range_total = sum(days.values())
    top_airlines = _airline_rows(_top(agg["airlines"], 25))
    top_tails = heapq.nsmallest(25, agg["tails"].items(), key=lambda kv: (-kv[1][0], kv[0]))
    return {
        "today_total":     sum(t_agg["days"].values()) if t_agg is not None else None,
        "today_top":       _airline_rows(_top(t_agg["airlines"], 5)) if t_agg is not None else [],
        "today_api_calls": {api: total for api, total in today_ac_rows},
        "today_unique":    len(t_agg["tails"]) if t_agg is not None else None,
        "today_hourly":    list(t_agg["hourly"]) if t_agg is not None else None,
        "yesterday_times": y_times,
        "range_total":     range_total,
        "range_top":       top_airlines,
        "range_api_calls": {api: total for api, total in ac_rows},
        "range_unique":    len(agg["tails"]),
        "day_api_calls":   day_api_calls,
        "hourly":          list(agg["hourly"]),
        "recent":          [_sighting_dict(r) for r in recent],
        "rollup": {
            "flights":    range_total,
            "airlines":   top_airlines,
            "tails":      [
                {
                    "reg":   reg,
                    "count": t[0],
                    "name":  ", ".join(filter(None, [AIRLINE_NAMES.get(t[1], ""), t[2]])),
                }
                for reg, t in top_tails
            ],
            "routes":     [{"route": f"{o}→{d}", "count": c} for (o, d), c in _top(agg["routes"], 50)],
            "types":      [{"type": t, "count": c} for t, c in _top(agg["types"], 25)],
            "source_pct": source_pct,
        },
        "day_counts": dict(days),
    }


def ensure_indexes() -> None:
    """Idempotent, once per web start: the seen_at index that lets "newest first" queries
    (recent flights, search paging) walk an index instead of sorting every matching row.
    Databases created before 2026-11 don't have it.  Never raises: a busy DB just tries again
    on the next start."""
    if DB_FILE is None or not DB_FILE.exists():
        return
    conn = None
    try:
        conn = sqlite3.connect(str(DB_FILE), timeout=10)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_seen_at ON sightings(seen_at)")
        conn.commit()
    except Exception as exc:
        _log(f"[web] index check skipped: {exc}")
    finally:
        if conn:
            conn.close()


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
