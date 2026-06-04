#!/usr/bin/env python3
"""
backfill_resolved_cache.py — Populate the 'resolved' route cache from historical sightings.

For every scheduled-airline callsign that has a complete route (origin + destination)
in the sightings table, insert a 7-day 'resolved' cache entry so the live lookup
skips the full API chain for known daily flights.

Only routes with a LOCAL endpoint are written (mirrors overhead.py's own resolved-write
guard) — a non-local, coordless entry would just be busted + re-billed on the next poll.

Safe to re-run: existing fresh entries are left alone unless --force is passed.  --force
rewrites every matching entry's TTL to now+7d, so it requires confirmation (or --yes).
Use --dry-run to preview without writing.  A timestamped DB backup is made before writes.

Usage (on the Pi):
    python3 .../backfill_resolved_cache.py [--verbose] [--dry-run]
    python3 .../backfill_resolved_cache.py --force [--yes]
    python3 .../backfill_resolved_cache.py --db /path/to/ft_flights.db
"""

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
_SCRIPT_DIR  = Path(__file__).resolve().parent
_PROJECT_DIR = _SCRIPT_DIR.parent
DB_FILE      = _PROJECT_DIR / "ft_flights.db"

# ── Constants (must match overhead.py) ────────────────────────────────────────
ROUTE_TTL_SCHEDULED = 604800  # 7 days

# The scheduled-airline prefix set is the single source of truth in utilities/refdata.py
# (pure data, no import-time side effects — it does NOT open the DB or read config.py the
# way overhead.py does), shared between the live resolver and this maintenance script so
# the two can never drift.  Imported and re-exported here so backfill_rc._SCHEDULED_PREFIXES
# stays available to callers/tests.  Ensure the project root is importable as a package root
# even when this script is run standalone from the utilities/ dir.
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))
from utilities.refdata import _SCHEDULED_PREFIXES  # noqa: E402  (re-exported below)


def _is_scheduled(callsign: str) -> bool:
    return len(callsign) >= 3 and callsign[:3].upper() in _SCHEDULED_PREFIXES


def _local_airports() -> frozenset:
    """Read LOCAL_AIRPORTS from config.py (best-effort) so we only persist routes the live
    resolver would also keep.  Empty set (or no config) → the locality gate is skipped."""
    try:
        sys.path.insert(0, str(_PROJECT_DIR))
        import config
        raw = getattr(config, "LOCAL_AIRPORTS", "") or getattr(config, "LOCAL_AIRPORT", "")
        return frozenset(a.strip().upper() for a in str(raw).split(",") if a.strip())
    except Exception:
        return frozenset()


def _backup_db(db_path: Path) -> None:
    """Timestamped copy of the DB before any writes, so a bad run can be undone."""
    if not db_path.exists():
        return
    backup = db_path.with_name(f"{db_path.name}-{time.strftime('%Y%m%d-%H%M%S')}.bak")
    try:
        shutil.copy2(db_path, backup)
        print(f"Backup written: {backup}")
    except Exception as exc:
        print(f"WARNING: could not back up DB ({exc}); continuing without backup", file=sys.stderr)


def backfill(db_path: Path, verbose: bool = False, force: bool = False,
             dry_run: bool = False, assume_yes: bool = False) -> None:
    if not db_path.exists():
        print(f"ERROR: database not found: {db_path}")
        return

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")   # wait (don't error) if the live service is mid-write

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

    # ── Step 3: Filter to scheduled-airline prefixes with a LOCAL endpoint ──────
    # The locality gate mirrors overhead.py's resolved-write guard: a non-local route
    # written with NULL coords would just be busted + re-resolved on the next live poll,
    # wasting a delete + a paid re-lookup.  Empty LOCAL_AIRPORTS → gate skipped.
    locals_ = _local_airports()

    def _keep(cs: str, route: tuple) -> bool:
        if not _is_scheduled(cs):
            return False
        if locals_ and not (route[0] in locals_ or route[1] in locals_):
            return False
        return True

    scheduled = {cs: route for cs, route in best.items() if _keep(cs, route)}
    if not locals_:
        print("WARNING: LOCAL_AIRPORTS not found in config — locality gate skipped "
              "(writing all scheduled routes).", file=sys.stderr)

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

    # ── Guard rails: dry-run preview, --force confirmation, pre-write backup ───
    to_write = [cs for cs in scheduled if cs not in already_fresh]
    if dry_run:
        print("DRY RUN — no changes will be written.")
        print(f"  Would write {len(to_write)} resolved entries "
              f"({'force-refresh' if force else 'new only'}); "
              f"{len(scheduled) - len(to_write)} already fresh (skipped).")
        if verbose:
            for cs in to_write:
                o, d = scheduled[cs]
                print(f"    would write {cs:12s} {o}->{d}")
        conn.close()
        return
    if force and not assume_yes:
        if not sys.stdin.isatty():
            print("Refusing --force without --yes in a non-interactive run.", file=sys.stderr)
            conn.close()
            return
        if input(f"--force will rewrite {len(to_write)} entries' TTL to now+7d. Continue? [yes/no] "
                 ).strip().lower() not in ("yes", "y"):
            print("Aborted.")
            conn.close()
            return
    _backup_db(db_path)

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
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview what would change without writing")
    parser.add_argument("--yes", action="store_true",
                        help="Skip the --force confirmation prompt (for non-interactive runs)")
    args = parser.parse_args()

    backfill(Path(args.db), verbose=args.verbose, force=args.force,
             dry_run=args.dry_run, assume_yes=args.yes)
