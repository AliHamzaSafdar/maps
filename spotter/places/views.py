import json

from django.http import JsonResponse
from django.shortcuts import render

from . import aws_routes, osrm_routes
from .geocode import geocode, reverse_country, suggest
from .geo import (
    DEFAULT_RADIUS_KM,
    DEFAULT_RANGE_MILES,
    DEFAULT_TRUCK_MPG,
    in_usa,
    metrics_from_legs,
    plan_fuel_stops,
    stops_along_route,
)
from .models import Place

ROUTE_COLORS = ["#16a34a", "#2563eb", "#9333ea"]  # cheapest gets green
PRICE_FIELDS = {"average": "average_retail_price", "max": "highest_price"}


def _parse_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _selected_coords(request, prefix):
    """(lon, lat) chosen from an autocomplete suggestion, or None."""
    lon = _parse_float(request.GET.get(f"{prefix}_sel_lon"))
    lat = _parse_float(request.GET.get(f"{prefix}_sel_lat"))
    return (lon, lat) if lon is not None and lat is not None else None


def suggest_view(request):
    """JSON location suggestions for the autocomplete dropdown.

    Always uses the free OpenStreetMap (Nominatim) geocoder, regardless of the
    selected routing provider — this is just typeahead, like Google Maps search.
    """
    query = request.GET.get("q", "").strip()
    if len(query) < 3:
        return JsonResponse({"results": []})
    try:
        results = suggest(query, "free")
    except Exception as exc:
        return JsonResponse({"results": [], "error": str(exc)}, status=200)
    return JsonResponse({"results": results})


def _resolve_endpoints(request, provider):
    """Return ((start_lon, start_lat), (end_lon, end_lat), error).

    Honours the input-mode dropdown: either parse coordinates directly or
    geocode the typed place names with the chosen provider.
    """
    if request.GET.get("input_mode") == "location":
        start_q = request.GET.get("start_location", "").strip()
        end_q = request.GET.get("end_location", "").strip()
        if not (start_q and end_q):
            return None, None, None  # incomplete; not an error yet
        # Use coords from a picked autocomplete suggestion when present, so the
        # user gets exactly the place they chose rather than a re-geocode guess.
        start_sel = _selected_coords(request, "start")
        end_sel = _selected_coords(request, "end")
        try:
            start = start_sel or geocode(start_q, provider)
            end = end_sel or geocode(end_q, provider)
        except Exception as exc:
            return None, None, f"Geocoding failed: {exc}"
        if not start:
            return None, None, f"Could not find a location for '{start_q}'."
        if not end:
            return None, None, f"Could not find a location for '{end_q}'."
        if not in_usa(*start):
            return None, None, f"'{start_q}' is outside the US. Both locations must be in the US."
        if not in_usa(*end):
            return None, None, f"'{end_q}' is outside the US. Both locations must be in the US."
        return start, end, None

    coords = [
        _parse_float(request.GET.get(k))
        for k in ("start_lon", "start_lat", "end_lon", "end_lat")
    ]
    if None in coords:
        return None, None, None  # incomplete
    start, end = (coords[0], coords[1]), (coords[2], coords[3])
    # Raw coordinates skip the geocoder's country filter, so verify each point
    # is in the US ourselves.
    err = _coord_us_error(start, "Start", provider) or _coord_us_error(end, "End", provider)
    if err:
        return None, None, err
    return start, end, None


def _coord_us_error(point, label, provider):
    """Error string if a manually-entered (lon, lat) is outside the US, else None.

    Fast-rejects far-away points with a bounding box, then reverse-geocodes the
    survivors to catch border-region points (e.g. Toronto) the box can't. A
    geocoder hiccup falls back to the box result so a transient failure doesn't
    block a legitimate US point.
    """
    if not in_usa(*point):
        return f"{label} location is outside the US. Both locations must be in the US."
    try:
        country = reverse_country(point[0], point[1], provider)
    except Exception:
        country = None
    if country is not None and country not in ("us", "usa"):
        return f"{label} location is in '{country.upper()}', not the US. Both locations must be in the US."
    return None


def planner(request):
    """Render the route planner; compute routes + fuel stops when input is given."""
    provider = request.GET.get("provider", "aws")
    input_mode = request.GET.get("input_mode", "coords")
    price_choice = request.GET.get("price_field", "average")
    price_attr = PRICE_FIELDS.get(price_choice, "average_retail_price")

    ctx = {
        "form": {
            "provider": provider,
            "input_mode": input_mode,
            "price_field": price_choice,
            "start_lon": request.GET.get("start_lon", ""),
            "start_lat": request.GET.get("start_lat", ""),
            "end_lon": request.GET.get("end_lon", ""),
            "end_lat": request.GET.get("end_lat", ""),
            "start_location": request.GET.get("start_location", ""),
            "end_location": request.GET.get("end_location", ""),
            "start_sel_lon": request.GET.get("start_sel_lon", ""),
            "start_sel_lat": request.GET.get("start_sel_lat", ""),
            "end_sel_lon": request.GET.get("end_sel_lon", ""),
            "end_sel_lat": request.GET.get("end_sel_lat", ""),
            "mpg": request.GET.get("mpg", DEFAULT_TRUCK_MPG),
            "range_miles": request.GET.get("range_miles", DEFAULT_RANGE_MILES),
            "radius_km": request.GET.get("radius_km", DEFAULT_RADIUS_KM),
            "max_routes": request.GET.get("max_routes", 1),
        },
        "results_json": "null",
        "error": None,
    }

    mpg = _parse_float(request.GET.get("mpg")) or DEFAULT_TRUCK_MPG
    range_miles = _parse_float(request.GET.get("range_miles")) or DEFAULT_RANGE_MILES
    radius_km = _parse_float(request.GET.get("radius_km")) or DEFAULT_RADIUS_KM
    # Default 1 route; allow up to 3 alternatives. Clamp so URL-tampering can't
    # ask the router for an unbounded number of routes.
    max_routes = int(_parse_float(request.GET.get("max_routes")) or 1)
    max_routes = max(1, min(max_routes, 3))

    start, end, error = _resolve_endpoints(request, provider)
    if error:
        ctx["error"] = error
        return render(request, "places/planner.html", ctx)
    if start is None or end is None:
        return render(request, "places/planner.html", ctx)  # first load / incomplete

    router = aws_routes if provider == "aws" else osrm_routes
    try:
        # More alternatives = more per-stop scanning, so this is user-capped at 3.
        raw_routes = router.get_routes(start[0], start[1], end[0], end[1], max_routes=max_routes)
    except Exception as exc:  # surface provider/credential errors to the user
        ctx["error"] = f"Routing failed ({provider}): {exc}"
        return render(request, "places/planner.html", ctx)

    if not raw_routes:
        ctx["error"] = "No routes found between those points."
        return render(request, "places/planner.html", ctx)

    all_stops = list(
        Place.objects.filter(
            geocoded_lat__isnull=False, geocoded_lon__isnull=False
        )
    )

    results = []
    for route in raw_routes:
        along = stops_along_route(all_stops, route["coordinates"], radius_km)
        plan = plan_fuel_stops(along, route["distance_km"], range_miles, mpg, price_attr)
        selected = plan["fuel_stops"]  # already in along (travel) order

        # Geometry + metrics default to the straight route; Pass 2 below replaces
        # them with a path that actually detours through each chosen pump.
        geometry = route["coordinates"]
        distance_km = route["distance_km"]
        duration_s = route["duration_s"]
        metrics = plan

        # Pass 2 — re-route through the pumps so the line visibly visits them and
        # the cost reflects the real detour. One extra routing call per route that
        # has stops (so the default single route adds just one call); skipped when
        # there are no stops to visit.
        if selected:
            waypoints = [list(start)] + [[c["lon"], c["lat"]] for c in selected] + [list(end)]
            try:
                via = router.get_route_via(waypoints)
            except Exception:
                via = None  # provider hiccup → keep the straight route, never error out
            if via and len(via["legs_km"]) == len(selected) + 1:
                geometry = via["coordinates"]
                distance_km = via["distance_km"]
                duration_s = via["duration_s"]
                metrics = metrics_from_legs(
                    via["legs_km"], [c["price"] for c in selected], range_miles, mpg
                )

        fuel_stops = [
            {
                "order": c["order"],
                "name": c["stop"].name,
                "city": c["stop"].city,
                "state": c["stop"].state,
                "lon": c["lon"],  # real station location; the route now detours to it
                "lat": c["lat"],
                "price": round(c["price"], 3),
                "avg_price": float(c["stop"].average_retail_price) if c["stop"].average_retail_price is not None else None,
                "max_price": float(c["stop"].highest_price) if c["stop"].highest_price is not None else None,
                "along_miles": round(c["along_km"] / 1.609344, 1),
                "off_km": round(c["off_km"], 1),
            }
            for c in selected
        ]

        results.append(
            {
                "coordinates": geometry,
                "distance_km": round(distance_km, 1),
                "distance_miles": round(distance_km / 1.609344, 1),
                "duration_h": round(duration_s / 3600.0, 1),
                "stops_available": len(along),
                "feasible": metrics["feasible"],
                "total_gallons": round(metrics["total_gallons"], 1),
                "total_cost": round(metrics["total_cost"], 2) if metrics["total_cost"] is not None else None,
                "arrival_gallons": round(metrics["arrival_gallons"], 1) if metrics["arrival_gallons"] is not None else None,
                "tank_capacity_gallons": round(metrics["tank_capacity_gallons"], 1),
                "fuel_stops": fuel_stops,
            }
        )

    # Cheapest total fuel cost first; infeasible / no-cost routes sort to the end.
    results.sort(
        key=lambda r: (not r["feasible"], r["total_cost"] is None, r["total_cost"] or 0)
    )
    for i, r in enumerate(results):
        r["color"] = ROUTE_COLORS[i % len(ROUTE_COLORS)]
        r["label"] = f"Route {i + 1}" + (" (cheapest)" if i == 0 else "")

    ctx["results_json"] = json.dumps(
        {
            "origin": list(start),
            "destination": list(end),
            "provider": provider,
            "price_field": price_choice,
            "range_miles": range_miles,
            "mpg": mpg,
            "routes": results,
        }
    )
    if request.GET.get("format") == "json" or "application/json" in request.headers.get("Accept", ""):
        return JsonResponse(json.loads(ctx["results_json"]))

    return render(request, "places/planner.html", ctx)

