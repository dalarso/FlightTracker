#!/usr/bin/env python3
"""
backfill_db.py — Parse plane.log and populate ft_flights.db from historical data.

Safe to re-run: re-running fills in any BLANK fields on existing rows — it never
overwrites data the live service has since enriched, and never changes row ids.
A timestamped DB backup is written before any changes.

Usage (on the Pi):
    python3 /home/pi/FlightTracker/utilities/backfill_db.py [--log /path/to/plane.log]

The script auto-locates ft_flights.db relative to this file's parent directory.
"""

import re
import shutil
import sqlite3
import sys
import time
import argparse
from pathlib import Path

# ── File paths ────────────────────────────────────────────────────────────────
_SCRIPT_DIR  = Path(__file__).resolve().parent
_PROJECT_DIR = _SCRIPT_DIR.parent
DB_FILE      = _PROJECT_DIR / "ft_flights.db"
DEFAULT_LOG  = Path.home() / "plane.log"

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
    conn.execute("PRAGMA busy_timeout=5000")   # wait (don't error) if the live service is mid-write
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


def _backup_db(db_path: Path) -> None:
    """Write a timestamped copy of the DB before any changes, so a bad run can be undone.
    Best-effort (ideally stop the FlightTracker service first so the DB is quiescent)."""
    if not db_path.exists():
        return
    backup = db_path.with_name(f"{db_path.name}-{time.strftime('%Y%m%d-%H%M%S')}.bak")
    try:
        shutil.copy2(db_path, backup)
        print(f"Backup written: {backup}")
    except Exception as exc:
        print(f"WARNING: could not back up DB ({exc}); continuing without backup", file=sys.stderr)


def backfill(log_path: Path, db_path: Path, verbose: bool = False) -> None:
    if not log_path.exists():
        print(f"ERROR: log not found: {log_path}", file=sys.stderr)
        sys.exit(1)

    conn = _open_db(db_path)

    inserted = 0
    updated  = 0
    errors   = 0
    drift    = 0   # route-format lines we failed to parse (log-format-drift signal)

    with open(log_path, "r", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip()
            if "[TEST:" in line:
                continue
            m = _ROUTE_RE.match(line)
            if not m:
                if "[route:" in line:      # looks like a route line but didn't parse → drift
                    drift += 1
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
                existed = conn.execute(
                    "SELECT 1 FROM sightings WHERE date=? AND callsign=?",
                    (date, callsign),
                ).fetchone() is not None
                # Fill only BLANK fields on an existing row — never overwrite data the live
                # service enriched after this log line, and preserve the row id (the old
                # INSERT OR REPLACE deleted+reinserted, reverting enrichment + churning ids).
                conn.execute(
                    """
                    INSERT INTO sightings
                        (seen_at, date, callsign, registration,
                         origin, destination, aircraft, route_source, airline)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(date, callsign) DO UPDATE SET
                        registration = CASE WHEN sightings.registration='' THEN excluded.registration ELSE sightings.registration END,
                        origin       = CASE WHEN sightings.origin=''       THEN excluded.origin       ELSE sightings.origin       END,
                        destination  = CASE WHEN sightings.destination=''  THEN excluded.destination  ELSE sightings.destination  END,
                        aircraft     = CASE WHEN sightings.aircraft=''     THEN excluded.aircraft     ELSE sightings.aircraft     END,
                        route_source = CASE WHEN sightings.route_source='' THEN excluded.route_source ELSE sightings.route_source END,
                        airline      = CASE WHEN sightings.airline=''      THEN excluded.airline      ELSE sightings.airline      END
                    """,
                    (seen_at, date, callsign, registration,
                     origin, dest, aircraft, route_src, prefix),
                )
                if existed:
                    updated += 1
                else:
                    inserted += 1
                    if verbose:
                        print(f"  + {seen_at} {callsign:10s} {origin or '?'}→{dest or '?'}  '{aircraft}'")
            except Exception as exc:
                errors += 1
                if verbose:
                    print(f"  ! {callsign}: {exc}")

    conn.commit()
    conn.close()

    print("Backfill complete:")
    print(f"  Inserted (new)         : {inserted}")
    print(f"  Updated (filled blanks): {updated}")
    print(f"  Errors                 : {errors}")
    if drift:
        print(f"  WARNING: {drift} route-format lines did not parse — log format may have drifted")
    print(f"  DB                     : {db_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill ft_flights.db sightings from plane.log")
    parser.add_argument("--log",     default=str(DEFAULT_LOG), help="Path to plane.log")
    parser.add_argument("--db",      default=str(DB_FILE),     help="Path to ft_flights.db")
    parser.add_argument("--verbose", action="store_true",       help="Print each inserted row")
    args = parser.parse_args()

    db_path = Path(args.db)
    _backup_db(db_path)   # timestamped safety copy before any writes
    backfill(Path(args.log), db_path, verbose=args.verbose)
