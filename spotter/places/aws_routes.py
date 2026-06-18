"""Thin wrapper around AWS Location Service Routes (geo-routes) v2.

Returns up to `1 + max_alternatives` driving routes for a truck, each as a list
of (lon, lat) coordinates plus distance/duration. Credentials and region come
from the standard AWS chain (~/.aws/config, env vars, instance role).
"""

import boto3

from .geo import haversine_km

# AWS caps MaxAlternatives at 6; we want at most 3 routes total.
MAX_ROUTES = 3


def _client():
    return boto3.client("geo-routes")


def get_routes(origin_lon, origin_lat, dest_lon, dest_lat, max_routes=MAX_ROUTES):
    """Fetch driving routes from origin to destination.

    Coordinates are floats in (lon, lat) order. Returns a list of dicts:
    {"coordinates": [[lon, lat], ...], "distance_km": float, "duration_s": int}
    sorted as AWS returns them (primary route first).
    """
    max_alternatives = max(0, min(max_routes, MAX_ROUTES) - 1)
    resp = _client().calculate_routes(
        Origin=[origin_lon, origin_lat],
        Destination=[dest_lon, dest_lat],
        TravelMode="Truck",
        LegGeometryFormat="Simple",
        MaxAlternatives=max_alternatives,
    )

    routes = []
    for route in resp.get("Routes", []):
        coords = []
        for leg in route.get("Legs", []):
            line = leg.get("Geometry", {}).get("LineString", [])
            coords.extend([lon, lat] for lon, lat in line)
        if not coords:
            continue
        summary = route.get("Summary", {})
        routes.append(
            {
                "coordinates": coords,
                "distance_km": summary.get("Distance", 0) / 1000.0,
                "duration_s": summary.get("Duration", 0),
            }
        )
    return routes[:max_routes]


def get_route_via(waypoints):
    """Route through an ordered list of (lon, lat) waypoints (start, …, end).

    Used to make the drawn route actually detour to each chosen fuel stop.
    Returns {"coordinates", "distance_km", "duration_s", "legs_km"} or None.

    AWS doesn't return a clean per-leg distance, so each leg's length is measured
    from its own geometry and then scaled so the legs sum to AWS's authoritative
    route total — keeping leg ratios while matching the trusted overall distance.
    """
    origin = waypoints[0]
    dest = waypoints[-1]
    intermediate = waypoints[1:-1]
    resp = _client().calculate_routes(
        Origin=[origin[0], origin[1]],
        Destination=[dest[0], dest[1]],
        Waypoints=[{"Position": [lon, lat]} for lon, lat in intermediate],
        TravelMode="Truck",
        LegGeometryFormat="Simple",
        MaxAlternatives=0,
    )

    routes = resp.get("Routes", [])
    if not routes:
        return None
    route = routes[0]

    coords = []
    legs_km = []
    for leg in route.get("Legs", []):
        line = leg.get("Geometry", {}).get("LineString", [])
        if not line:
            return None  # missing a leg → can't price legs reliably; fall back
        coords.extend([lon, lat] for lon, lat in line)
        leg_km = sum(
            haversine_km(a[0], a[1], b[0], b[1]) for a, b in zip(line, line[1:])
        )
        legs_km.append(leg_km)

    if not coords:
        return None

    total_km = route.get("Summary", {}).get("Distance", 0) / 1000.0
    measured = sum(legs_km)
    if total_km > 0 and measured > 0:
        legs_km = [d * total_km / measured for d in legs_km]  # scale to the trusted total
    else:
        total_km = measured

    return {
        "coordinates": coords,
        "distance_km": total_km,
        "duration_s": route.get("Summary", {}).get("Duration", 0),
        "legs_km": legs_km,
    }
