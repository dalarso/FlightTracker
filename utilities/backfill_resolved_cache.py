#!/usr/bin/env python3
"""
backfill_resolved_cache.py — Populate the 'resolved' route cache from historical sightings.

For every scheduled-airline callsign that has a complete route (origin + destination)
in the sightings table, insert a 7-day 'resolved' cache entry so the live lookup
skips the full API chain for known daily flights.

Safe to re-run: INSERT OR REPLACE is idempotent.  Existing fresh entries are left
alone unless --force is passed (which rewrites their TTL to now+7d regardless).

Usage (on the Pi):
    python3 /home/pi/FlightTracker/utilities/backfill_resolved_cache.py
    python3 /home/pi/FlightTracker/utilities/backfill_resolved_cache.py --verbose
    python3 /home/pi/FlightTracker/utilities/backfill_resolved_cache.py --force
    python3 /home/pi/FlightTracker/utilities/backfill_resolved_cache.py --db /path/to/ft_flights.db
"""

import argparse
import sqlite3
import time
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
_SCRIPT_DIR  = Path(__file__).resolve().parent
_PROJECT_DIR = _SCRIPT_DIR.parent
DB_FILE      = _PROJECT_DIR / "ft_flights.db"

# ── Constants (must match overhead.py) ────────────────────────────────────────
ROUTE_TTL_SCHEDULED = 604800  # 7 days

# All prefixes that get the 7-day TTL — must stay in sync with overhead.py
_SCHEDULED_PREFIXES: frozenset[str] = frozenset([
    # US majors
    "AAL", "DAL", "UAL", "SWA", "ASA", "JBU", "NKS", "FFT", "SCX", "AAY",
    "HAL", "VRD",
    # US ULCCs / leisure / charter
    "MXY", "VXP", "JSX", "TWY",
    # Canadian regional / leisure
    "ROU",
    # US cargo (scheduled routes — 7-day TTL appropriate)
    "FDX", "UPS", "GTI", "ABX", "ASN", "PAC", "CKS", "WGN", "NCR", "SOU",
    "DHK", "AGX",
    # US charters / military contract
    "OCN", "OAE",
    # Canadian
    "ACA", "WJA", "POE", "FLE", "SWG",
    # Mexican
    "AMX", "VOI", "VIV",
    # European
    "BAW", "VIR", "AFR", "DLH", "KLM", "UAE", "QTR", "SIA", "EIN", "IBE",
    "CFG", "EDW", "THY", "ETD", "SWR", "AUA", "NAX", "EZY", "RYR", "TAP",
    "FIN", "BEL",
    # Asian / Pacific
    "KAL", "ANA", "JAL", "CPA", "EVA", "CCA", "CSN", "ANZ",
    # Latin American
    "CMP", "AVA",
    # Oceania
    "QFA",
    # Regional/commuter
    "SKW", "ENY", "RPA", "QXE", "ASH", "PDT", "JIA", "UCA", "CPZ", "MTN",
    "FLG",
])


def _is_scheduled(callsign: str) -> bool:
    return len(callsign) >= 3 and callsign[:3].upper() in _SCHEDULED_PREFIXES


def backfill(db_path: Path, verbose: bool = False, force: bool = False) -> None:
    if not db_path.exists():
        print(f"ERROR: database not found: {db_path}")
        return

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    # Ensure cache table exists (mirrors _init_db in overhead.py exactly)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cache (
            key        TEXT    NOT NULL,
            cache_type TEXT    NOT NULL,
            origin     TEXT    NOT NULL DEFAULT '',
            dest       TEXT    NOT NULL DEFAULT '',
            olat       REAL,
            olon       REAL,
            dlat       REAL,
            dlon       REAL,
            value      TEXT    NOT NULL DEFAULT '',
            source     TEXT    NOT NULL DEFAULT '',
            expires_at INTEGER NOT NULL,
            PRIMARY KEY (key, cache_type)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache(expires_at)
    """)
    conn.commit()

    now = int(time.time())
    expires = now + ROUTE_TTL_SCHEDULED

    # ── Step 1: Load all sightings with a complete route, newest first ─────────
    rows = conn.execute("""
        SELECT callsign, origin, destination
        FROM   sightings
        WHERE  origin != '' AND destination != ''
        ORDER  BY seen_at DESC
    """).fetchall()

    # ── Step 2: Dedup — keep only the most recent route per callsign ───────────
    # Because rows are ordered newest-first, the first time we see a callsign
    # is its most recent observed route.
    best: dict[str, tuple[str, str]] = {}
    for callsign, origin, dest in rows:
        if callsign not in best:
            best[callsign] = (origin, dest)

    # ── Step 3: Filter to scheduled-airline prefixes ───────────────────────────
    scheduled = {cs: route for cs, route in best.items() if _is_scheduled(cs)}

    # ── Step 4: Check which already have a fresh 'resolved' entry ─────────────
    already_fresh: set[str] = set()
    if not force:
        for cs in scheduled:
            row = conn.execute(
                "SELECT expires_at FROM cache WHERE key=? AND cache_type='resolved'",
                (cs,),
            ).fetchone()
            if row and row[0] > now:
                already_fresh.add(cs)

    # ── Step 5: Insert resolved cache entries ─────────────────────────────────
    inserted = 0
    refreshed = 0
    skipped   = 0

    for cs, (origin, dest) in scheduled.items():
        if cs in already_fresh:
            skipped += 1
            if verbose:
                print(f"  ~ {cs:12s} {origin}→{dest}  (already fresh — skipping)")
            continue

        was_force = force and cs not in already_fresh
        conn.execute(
            """
            INSERT OR REPLACE INTO cache
                (key, cache_type, origin, dest, olat, olon, dlat, dlon, source, expires_at)
            VALUES (?, 'resolved', ?, ?, NULL, NULL, NULL, NULL, 'backfill', ?)
            """,
            (cs, origin, dest, expires),
        )
        if verbose:
            tag = "force-refresh" if was_force else "+"
            print(f"  {tag} {cs:12s} {origin}→{dest}  (expires in 7d)")
        if was_force:
            refreshed += 1
        else:
            inserted += 1

    conn.commit()
    conn.close()

    total_sightings = len(rows)
    unique_callsigns = len(best)
    print(f"Resolved-cache backfill complete")
    print(f"  Sighting rows read     : {total_sightings}")
    print(f"  Unique callsigns (routed): {unique_callsigns}")
    print(f"  Scheduled-airline matches: {len(scheduled)}")
    print(f"  Inserted (new)         : {inserted}")
    print(f"  Force-refreshed        : {refreshed}")
    print(f"  Skipped (already fresh): {skipped}")
    print(f"  DB                     : {db_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backfill 'resolved' route cache from historical sightings"
    )
    parser.add_argument("--db",      default=str(DB_FILE), help="Path to ft_flights.db")
    parser.add_argument("--verbose", action="store_true",  help="Print each entry")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rewrite TTL even for already-fresh entries (sets all to now+7d)",
    )
    args = parser.parse_args()

    backfill(Path(args.db), verbose=args.verbose, force=args.force)
