"""Thin wrapper around AWS Location Service Routes (geo-routes) v2.

Returns up to `1 + max_alternatives` driving routes for a truck, each as a list
of (lon, lat) coordinates plus distance/duration. Credentials and region come
from the standard AWS chain (~/.aws/config, env vars, instance role).
"""

import boto3

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
