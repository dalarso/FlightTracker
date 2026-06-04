"""Pure great-circle geometry + the route-plausibility (detour-ratio) guard.

Extracted from overhead.py: no I/O, no config, no project dependencies. overhead.py
imports _haversine_km and _route_plausible back from here, so `overhead.<name>` — and
the existing unit tests that call them — keep working unchanged.

EARTH_RADIUS_KM / _DEG2RAD are also defined in overhead.py (for its altitude + ECEF
helpers); they're identical universal constants, kept local here so this module stays
dependency-free.
"""
import math

EARTH_RADIUS_KM        = 6371
_DEG2RAD               = math.pi / 180
ROUTE_DETOUR_RATIO_MAX = 1.8   # reject when (plane→orig + plane→dest)/route ≥ this (PR #25)
ROUTE_SHORT_HOP_KM     = 80    # routes shorter than this skip the geometry check


def _haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km between two lat/lon points."""
    dlat = _DEG2RAD * (lat2 - lat1)
    dlon = _DEG2RAD * (lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(_DEG2RAD * lat1) * math.cos(_DEG2RAD * lat2)
         * math.sin(dlon / 2) ** 2)
    return EARTH_RADIUS_KM * 2 * math.asin(math.sqrt(max(0.0, min(1.0, a))))


def _route_plausible(plane_lat, plane_lon, orig_lat, orig_lon, dest_lat, dest_lon):
    """
    Return True if the aircraft's current position is geometrically consistent
    with the given route.  Uses the detour-ratio test from PR #25:

        (dist_plane→origin + dist_plane→dest) / dist_origin→dest < ROUTE_DETOUR_RATIO_MAX

    A value ≥ ROUTE_DETOUR_RATIO_MAX means the aircraft is far off the great-circle path —
    a strong signal that the API returned stale or wrong route data.

    Returns True when any coordinate is missing (benefit of the doubt).
    """
    if not all(v is not None for v in (plane_lat, plane_lon,
                                        orig_lat, orig_lon,
                                        dest_lat, dest_lon)):
        return True  # Can't validate — assume plausible

    route_km = _haversine_km(orig_lat, orig_lon, dest_lat, dest_lon)
    if route_km == 0:
        return False  # Same-airport route — reject; no valid flight path exists
    if route_km < ROUTE_SHORT_HOP_KM:
        return True  # Short hop — geometry check not reliable at this scale

    d_orig = _haversine_km(plane_lat, plane_lon, orig_lat, orig_lon)
    d_dest = _haversine_km(plane_lat, plane_lon, dest_lat, dest_lon)
    return (d_orig + d_dest) / route_km < ROUTE_DETOUR_RATIO_MAX
