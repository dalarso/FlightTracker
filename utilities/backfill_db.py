#!/usr/bin/env python3
"""
backfill_db.py — Parse plane.log and populate ft_flights.db from historical data.

Safe to re-run: INSERT OR REPLACE updates any row already in the DB, so re-running
always produces a fully up-to-date database.

Usage (on the Pi):
    python3 /home/pi/FlightTracker/utilities/backfill_db.py [--log /path/to/plane.log]

The script auto-locates ft_flights.db relative to this file's parent directory.
"""

import json
import re
import sqlite3
import sys
import argparse
from pathlib import Path

# ── File paths ────────────────────────────────────────────────────────────────
_SCRIPT_DIR  = Path(__file__).resolve().parent
_PROJECT_DIR = _SCRIPT_DIR.parent
DB_FILE      = _PROJECT_DIR / "ft_flights.db"
DEFAULT_LOG  = Path.home() / "plane.log"
DEFAULT_STATS = _PROJECT_DIR / "ft_stats.json"

# ── Log-line regexes ──────────────────────────────────────────────────────────
# Matches the core overhead log line:
#   [2026-05-11 22:04:09] [route:adsbdb] [type:airplanes.live] AAL437 (American) LAS->PHL ...
#
# Groups: (datetime_str, route_src, callsign, origin, destination)
# Note: [type:SOURCE] is intentionally non-capturing — the actual aircraft model
# is in the quoted string later on the line, captured by _AIRCRAFT_RE below.
_ROUTE_RE = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]"   # [1] timestamp
    r"\s+\[route:([^\]]*)\]"                           # [2] route source
    r"\s+\[type:[^\]]*\]"                              # type source (non-capturing)
    r"\s+([A-Z][A-Z0-9]{2,9})"                        # [3] callsign
    r"(?:\s+\([^)]*\))?"                               # optional (Airline Name)
    r"\s+([A-Z?]{3})->([A-Z?]{3})"                    # [4] origin  [5] destination
)

# Captures the actual aircraft model and trailing registration from:
#   ... 'BOEING 737-800' N889LS
_AIRCRAFT_RE = re.compile(r"'([^']*)'\s*([A-Z][A-Z0-9-]{3,})?\s*$")


def _open_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sightings (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            seen_at      TEXT NOT NULL,
            date         TEXT NOT NULL,
            callsign     TEXT NOT NULL,
            registration TEXT NOT NULL DEFAULT '',
            origin       TEXT NOT NULL DEFAULT '',
            destination  TEXT NOT NULL DEFAULT '',
            aircraft     TEXT NOT NULL DEFAULT '',
            route_source TEXT NOT NULL DEFAULT '',
            airline      TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_seen_cs
        ON sightings(date, callsign)
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_date         ON sightings(date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_callsign     ON sightings(callsign)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_registration ON sightings(registration)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_origin       ON sightings(origin)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_destination  ON sightings(destination)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_calls (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            date     TEXT NOT NULL,
            api_name TEXT NOT NULL,
            count    INTEGER NOT NULL DEFAULT 0,
            UNIQUE(date, api_name)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ac_date ON api_calls(date)")
    conn.commit()
    return conn


def backfill(log_path: Path, db_path: Path, verbose: bool = False) -> None:
    if not log_path.exists():
        print(f"ERROR: log not found: {log_path}", file=sys.stderr)
        sys.exit(1)

    conn = _open_db(db_path)

    inserted = 0
    skipped  = 0
    errors   = 0

    with open(log_path, "r", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip()
            if "[TEST:" in line:
                continue
            m = _ROUTE_RE.match(line)
            if not m:
                continue
            seen_at, route_src, callsign, origin, dest = m.groups()
            date   = seen_at[:10]
            prefix = callsign[:3].upper() if len(callsign) >= 3 else "???"

            # Pull actual aircraft model + registration from quoted part of line
            am = _AIRCRAFT_RE.search(line, m.end())
            aircraft     = am.group(1) if am else ""
            registration = (am.group(2) or "") if am else ""

            # Normalise "?" → empty string
            if origin == "?":   origin = ""
            if dest   == "?":   dest   = ""

            try:
                # INSERT OR REPLACE so re-runs update rows that had wrong aircraft
                # data from a previous version of this script.
                cur = conn.execute(
                    """
                    INSERT OR REPLACE INTO sightings
                        (seen_at, date, callsign, registration,
                         origin, destination, aircraft, route_source, airline)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (seen_at, date, callsign, registration,
                     origin, dest, aircraft, route_src, prefix),
                )
                if cur.rowcount:
                    inserted += 1
                    if verbose:
                        print(f"  + {seen_at} {callsign:10s} {origin or '?'}→{dest or '?'}  '{aircraft}'")
                else:
                    skipped += 1
            except Exception as exc:
                errors += 1
                if verbose:
                    print(f"  ! {callsign}: {exc}")

    conn.commit()
    conn.close()

    total = inserted + skipped + errors
    print(f"Backfill complete: {total} log lines matched")
    print(f"  Inserted : {inserted}")
    print(f"  Skipped  : {skipped}  (already in DB)")
    print(f"  Errors   : {errors}")
    print(f"  DB       : {db_path}")


def backfill_api_calls(stats_path: Path, db_path: Path, verbose: bool = False) -> None:
    """
    Read ft_stats.json and populate the api_calls table in ft_flights.db.
    Uses INSERT OR REPLACE so re-runs are idempotent.
    """
    if not stats_path.exists():
        print(f"SKIP: stats file not found: {stats_path}", file=sys.stderr)
        return

    try:
        with open(stats_path) as fh:
            stats = json.load(fh)
    except Exception as exc:
        print(f"ERROR: could not read {stats_path}: {exc}", file=sys.stderr)
        return

    conn = _open_db(db_path)
    inserted = 0
    skipped  = 0

    for date, rec in stats.items():
        # Skip keys that aren't date strings (YYYY-MM-DD)
        if len(date) != 10 or date[4] != "-":
            continue
        api_calls = rec.get("api_calls", {})
        for api_name, count in api_calls.items():
            if not isinstance(count, (int, float)) or count < 0:
                continue
            count = int(count)
            try:
                cur = conn.execute(
                    """
                    INSERT OR REPLACE INTO api_calls (date, api_name, count)
                    VALUES (?, ?, ?)
                    """,
                    (date, api_name, count),
                )
                if cur.rowcount:
                    inserted += 1
                    if verbose:
                        print(f"  + {date} {api_name}: {count}")
                else:
                    skipped += 1
            except Exception as exc:
                if verbose:
                    print(f"  ! {date} {api_name}: {exc}")

    conn.commit()
    conn.close()

    print(f"API calls backfill complete: {inserted} rows inserted, {skipped} skipped")
    print(f"  Stats file: {stats_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill ft_flights.db from plane.log + ft_stats.json")
    parser.add_argument("--log",     default=str(DEFAULT_LOG),   help="Path to plane.log")
    parser.add_argument("--stats",   default=str(DEFAULT_STATS), help="Path to ft_stats.json")
    parser.add_argument("--db",      default=str(DB_FILE),       help="Path to ft_flights.db")
    parser.add_argument("--verbose", action="store_true",         help="Print each inserted row")
    args = parser.parse_args()

    backfill(Path(args.log), Path(args.db), verbose=args.verbose)
    backfill_api_calls(Path(args.stats), Path(args.db), verbose=args.verbose)
