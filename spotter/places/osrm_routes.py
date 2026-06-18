"""Free routing via the public OSRM server (OpenStreetMap data).

Same interface and return shape as `aws_routes.get_routes` so the view can use
either provider interchangeably. Uses the car profile (the public demo server
has no truck profile), which is the trade-off for being free.
"""

import json
import urllib.parse
import urllib.request

OSRM_BASE = "https://router.project-osrm.org/route/v1/driving/"
MAX_ROUTES = 3
USER_AGENT = "GMAOS-route-planner/1.0"


def get_routes(origin_lon, origin_lat, dest_lon, dest_lat, max_routes=MAX_ROUTES):
    """Driving routes from origin to destination (coords in lon, lat order).

    Returns a list of dicts:
    {"coordinates": [[lon, lat], ...], "distance_km": float, "duration_s": float}
    """
    path = f"{origin_lon},{origin_lat};{dest_lon},{dest_lat}"
    query = urllib.parse.urlencode(
        {"alternatives": "true", "overview": "full", "geometries": "geojson"}
    )
    req = urllib.request.Request(
        f"{OSRM_BASE}{path}?{query}", headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())

    if data.get("code") != "Ok":
        return []

    routes = []
    for route in data.get("routes", []):
        coords = route.get("geometry", {}).get("coordinates", [])
        if not coords:
            continue
        routes.append(
            {
                "coordinates": [[lon, lat] for lon, lat in coords],
                "distance_km": route.get("distance", 0) / 1000.0,
                "duration_s": route.get("duration", 0),
            }
        )
    return routes[:max_routes]


def get_route_via(waypoints):
    """Route through an ordered list of (lon, lat) waypoints (start, …, end).

    Used to make the drawn route actually detour to each chosen fuel stop.
    Returns {"coordinates", "distance_km", "duration_s", "legs_km"} where
    legs_km is the real driving distance of each leg between consecutive
    waypoints, or None on failure (caller falls back to the straight route).
    """
    path = ";".join(f"{lon},{lat}" for lon, lat in waypoints)
    query = urllib.parse.urlencode({"overview": "full", "geometries": "geojson"})
    req = urllib.request.Request(
        f"{OSRM_BASE}{path}?{query}", headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())

    if data.get("code") != "Ok" or not data.get("routes"):
        return None
    route = data["routes"][0]
    coords = route.get("geometry", {}).get("coordinates", [])
    if not coords:
        return None
    return {
        "coordinates": [[lon, lat] for lon, lat in coords],
        "distance_km": route.get("distance", 0) / 1000.0,
        "duration_s": route.get("duration", 0),
        "legs_km": [leg.get("distance", 0) / 1000.0 for leg in route.get("legs", [])],
    }
