"""SQLite route / aircraft / registration cache — extracted from overhead.py.

The nine CRUD helpers below were lifted verbatim out of overhead.py.  overhead owns the
shared DB connection (opened in _init_db on ft_flights.db), the serialising lock, the
per-thread cache-bypass flag, the logger, and ROUTE_PAID_MISS_TTL; it injects them here
once via cache.bind() near the bottom of overhead.py (after _init_db()), then re-imports
the helpers so ``overhead._cache_db_*`` — and every caller and mocked test — resolve
unchanged.

Why dependency injection rather than importing overhead back here: the unit tests import
``overhead`` as a bare top-level module while the app imports it as ``utilities.overhead``,
so a back-import would spin up a *second* overhead module object with its own unbound DB
connection.  bind() sidesteps that by passing the live objects in.

The helpers never raise — a cache failure must never crash the poll loop.
"""
import time

# ── injected by overhead via bind(); placeholders until then ────────────────────
_cache_conn         = None    # sqlite3.Connection on ft_flights.db (shared with overhead)
_cache_lock         = None    # threading.Lock serialising every cache access
_cache_bypass       = None    # threading.local; .on=True masks reads during a forced refresh
ROUTE_PAID_MISS_TTL = 7200    # overridden from config by bind()

REG_CACHE_TTL = 365 * 24 * 3600  # 1 year — hex codes are permanent but evict if unseen

# Count of swallowed cache-WRITE failures (SQLITE_BUSY, disk-full, read-only SD card …).
# A slowly-failing SD card manifests as routes/types re-resolving every flyover; this
# counter (surfaced via the /api/health endpoint) makes that visible instead of silent.
_write_failures = 0


def write_failure_count() -> int:
    """Total cache-write failures since start — read by the health snapshot."""
    return _write_failures


def _log(_msg):   # replaced by overhead's real logger in bind()
    pass


def bind(conn, lock, bypass, log, route_paid_miss_ttl):
    """Inject overhead's shared cache state + config.  Called once, after _init_db()."""
    global _cache_conn, _cache_lock, _cache_bypass, _log, ROUTE_PAID_MISS_TTL
    _cache_conn   = conn
    _cache_lock   = lock
    _cache_bypass = bypass
    _log          = log
    ROUTE_PAID_MISS_TTL = route_paid_miss_ttl


def _cache_db_get_route(key: str, cache_type: str):
    """Return (origin, dest, olat, olon, dlat, dlon, source) for a fresh cache entry, or None."""
    if getattr(_cache_bypass, "on", False) or _cache_conn is None:
        return None
    try:
        with _cache_lock:
            row = _cache_conn.execute(
                """SELECT origin, dest, olat, olon, dlat, dlon, COALESCE(source, '')
                   FROM cache WHERE key=? AND cache_type=? AND expires_at>?""",
                (key, cache_type, int(time.time())),
            ).fetchone()
        return row
    except Exception:
        return None


def _cache_db_set_route(key: str, cache_type: str,
                         origin: str, dest: str,
                         olat, olon, dlat, dlon,
                         expires_at: int,
                         source: str = "") -> None:
    """Upsert a route/aeroapi cache entry, recording the originating API source."""
    if _cache_conn is None:
        return
    try:
        with _cache_lock:
            _cache_conn.execute(
                """INSERT OR REPLACE INTO cache
                   (key, cache_type, origin, dest, olat, olon, dlat, dlon, expires_at, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (key, cache_type, origin or '', dest or '',
                 olat, olon, dlat, dlon, expires_at, source or ''),
            )
            _cache_conn.commit()
    except Exception as _e:
        # A swallowed write here was the leading suspect for stale resolved entries
        # (Issue B): if the resolved-cache INSERT throws, the route silently re-resolves
        # every flyover.  Surface it instead of failing invisibly so a genuine DB-write
        # problem shows up in the log rather than as a phantom cache miss.
        global _write_failures
        _write_failures += 1
        _log(f"[cache] write failed for {cache_type}:{key} — {type(_e).__name__}: {_e}")


def _cache_db_delete_route(key: str, cache_type: str) -> None:
    """Delete a single route cache entry by key and type."""
    if _cache_conn is None:
        return
    try:
        with _cache_lock:
            _cache_conn.execute(
                "DELETE FROM cache WHERE key=? AND cache_type=?",
                (key, cache_type),
            )
            _cache_conn.commit()
    except Exception:
        pass


def _cache_db_get_aircraft(hex_code: str):
    """Return (type_str, source) for a fresh aircraft cache entry, or None."""
    if getattr(_cache_bypass, "on", False) or _cache_conn is None:
        return None
    try:
        with _cache_lock:
            row = _cache_conn.execute(
                """SELECT value, source FROM cache
                   WHERE key=? AND cache_type='aircraft' AND expires_at>?""",
                (hex_code, int(time.time())),
            ).fetchone()
        return row
    except Exception:
        return None


def _cache_db_set_aircraft(hex_code: str, type_str: str,
                            source: str, ttl: int) -> None:
    """Upsert an aircraft type cache entry."""
    if _cache_conn is None:
        return
    try:
        expires = int(time.time()) + ttl
        with _cache_lock:
            _cache_conn.execute(
                """INSERT OR REPLACE INTO cache
                   (key, cache_type, value, source, expires_at)
                   VALUES (?, 'aircraft', ?, ?, ?)""",
                (hex_code, type_str or '', source or '', expires),
            )
            _cache_conn.commit()
    except Exception as _e:
        global _write_failures
        _write_failures += 1
        _log(f"[cache] aircraft write failed for {hex_code} — {type(_e).__name__}: {_e}")


def _cache_db_get_reg(hex_code: str) -> str:
    """Return registration string for this hex code, or '' if not cached / expired."""
    if getattr(_cache_bypass, "on", False) or _cache_conn is None:
        return ''
    try:
        with _cache_lock:
            row = _cache_conn.execute(
                "SELECT value FROM cache WHERE key=? AND cache_type='reg' AND expires_at>?",
                (hex_code, int(time.time())),
            ).fetchone()
        return row[0] if row else ''
    except Exception:
        return ''


def _cache_db_set_reg(hex_code: str, reg: str) -> None:
    """Upsert a registration entry (1-year TTL — evicts aircraft not seen in a year)."""
    if _cache_conn is None:
        return
    try:
        with _cache_lock:
            _cache_conn.execute(
                """INSERT OR REPLACE INTO cache
                   (key, cache_type, value, expires_at)
                   VALUES (?, 'reg', ?, ?)""",
                (hex_code, reg, int(time.time()) + REG_CACHE_TTL),
            )
            _cache_conn.commit()
    except Exception as _e:
        global _write_failures
        _write_failures += 1
        _log(f"[cache] reg write failed for {hex_code} — {type(_e).__name__}: {_e}")


def _cache_db_check_paid_miss(callsign: str) -> bool:
    """Return True if callsign has a fresh paid-API-miss entry."""
    if _cache_conn is None or getattr(_cache_bypass, "on", False):
        return False
    try:
        with _cache_lock:
            row = _cache_conn.execute(
                """SELECT 1 FROM cache
                   WHERE key=? AND cache_type='paid_miss' AND expires_at>?""",
                (callsign, int(time.time())),
            ).fetchone()
        return row is not None
    except Exception:
        return False


def _cache_db_set_paid_miss(callsign: str) -> None:
    """Record that both paid APIs returned empty for callsign.

    No inline expired-row sweep: the periodic _purge_expired_cache (overhead.py) already
    reclaims expired rows across every cache_type, so deleting on every paid-miss write
    just added an extra write per call for no benefit (reads ignore expired rows anyway).
    """
    if _cache_conn is None:
        return
    try:
        expires = int(time.time()) + ROUTE_PAID_MISS_TTL
        with _cache_lock:
            _cache_conn.execute(
                """INSERT OR REPLACE INTO cache
                   (key, cache_type, expires_at)
                   VALUES (?, 'paid_miss', ?)""",
                (callsign, expires),
            )
            _cache_conn.commit()
    except Exception as _e:
        global _write_failures
        _write_failures += 1
        _log(f"[cache] paid_miss write failed for {callsign} — {type(_e).__name__}: {_e}")
