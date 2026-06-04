"""Stats / history data layer for the dashboard — extracted from server.py.

Pure SQLite + log-parsing helpers behind the /api/stats, /api/stats/search and
recent-sightings endpoints.  server.py owns the DB path and the stdout logger and
injects them via bind() once they exist; the four helpers are then re-imported so
the route handlers call them unchanged.

AIRLINE_NAMES and the two log-parse regexes moved here with the helpers — nothing
outside this module references them.
"""
import re
import sqlite3
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
            "SELECT registration, COUNT(*) cnt, "
            "MAX(airline) as airline, MAX(aircraft) as aircraft "
            "FROM sightings "
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
                "SELECT api_name, SUM(count) total FROM api_calls WHERE date = ? GROUP BY api_name",
                (today,),
            ).fetchall()
            today_ac = {r["api_name"]: r["total"] for r in today_ac_rows}

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
            "tails":      [
                {
                    "reg":   r["registration"],
                    "count": r["cnt"],
                    "name":  ", ".join(filter(None, [
                        AIRLINE_NAMES.get(r["airline"] or "", ""),
                        r["aircraft"] or "",
                    ])),
                }
                for r in tail_rows
            ],
            "routes":     [{"route": r["route"],       "count": r["cnt"]} for r in route_rows],
            "types":      [{"type": r["aircraft"],     "count": r["cnt"]} for r in type_rows],
            "source_pct": source_pct,
        },
        "day_counts": day_counts,
    }


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
