"""Pure geo + fuel-cost helpers for route planning.

No Django or AWS imports here so the math stays unit-testable in isolation.
Coordinates are (lon, lat) pairs to match AWS Location's ordering.
"""

from math import radians, sin, cos, asin, sqrt

EARTH_RADIUS_KM = 6371.0088
KM_PER_MILE = 1.609344

# Fuel model defaults.
DEFAULT_TRUCK_MPG = 10.0
DEFAULT_RANGE_MILES = 500.0
# Stops within this many km of the route line are considered "on the route".
DEFAULT_RADIUS_KM = 8.0


# Approximate bounding boxes covering US territory: (min_lon, min_lat, max_lon,
# max_lat). The contiguous box leaks a little into Canada/Mexico but is fine for
# a "is this roughly in the US?" guard; Alaska and Hawaii get their own boxes.
US_BBOXES = (
    (-125.0, 24.4, -66.9, 49.4),    # contiguous 48 states
    (-179.2, 51.0, -129.9, 71.5),   # Alaska
    (-160.3, 18.8, -154.7, 22.3),   # Hawaii
)


def in_usa(lon, lat):
    """True if (lon, lat) falls within any US bounding box."""
    return any(
        min_lon <= lon <= max_lon and min_lat <= lat <= max_lat
        for min_lon, min_lat, max_lon, max_lat in US_BBOXES
    )


def haversine_km(lon1, lat1, lon2, lat2):
    """Great-circle distance between two (lon, lat) points, in km."""
    lon1, lat1, lon2, lat2 = map(radians, (lon1, lat1, lon2, lat2))
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(a))


def cumulative_km(route_coords):
    """Running distance (km) along the route at each vertex; starts at 0."""
    cum = [0.0]
    for i in range(1, len(route_coords)):
        lon1, lat1 = route_coords[i - 1]
        lon2, lat2 = route_coords[i]
        cum.append(cum[-1] + haversine_km(lon1, lat1, lon2, lat2))
    return cum


def nearest_vertex(lon, lat, route_coords):
    """(distance_km, index) of the closest route vertex to a point."""
    best_d, best_i = None, 0
    for i, (rlon, rlat) in enumerate(route_coords):
        d = haversine_km(lon, lat, rlon, rlat)
        if best_d is None or d < best_d:
            best_d, best_i = d, i
    return best_d, best_i


def bounding_box(route_coords, buffer_km):
    """(min_lon, min_lat, max_lon, max_lat) around the route, padded by buffer_km."""
    lons = [c[0] for c in route_coords]
    lats = [c[1] for c in route_coords]
    # ~111 km per degree latitude; longitude shrinks with latitude.
    lat_pad = buffer_km / 111.0
    mid_lat = (min(lats) + max(lats)) / 2
    lon_pad = buffer_km / (111.0 * max(cos(radians(mid_lat)), 0.01))
    return (
        min(lons) - lon_pad,
        min(lats) - lat_pad,
        max(lons) + lon_pad,
        max(lats) + lat_pad,
    )


def downsample_route(route_coords, cum, step_km):
    """Thin a dense polyline to vertices ~step_km apart for proximity scans.

    Returns parallel lists (lons, lats, along_km). The first and last vertices
    are always kept so the route's full extent is covered.
    """
    keep = [0]
    last = 0.0
    for i in range(1, len(cum)):
        if cum[i] - last >= step_km:
            keep.append(i)
            last = cum[i]
    if keep[-1] != len(cum) - 1:
        keep.append(len(cum) - 1)
    lons = [route_coords[i][0] for i in keep]
    lats = [route_coords[i][1] for i in keep]
    alongs = [cum[i] for i in keep]
    return lons, lats, alongs


def stops_along_route(stops, route_coords, radius_km=DEFAULT_RADIUS_KM):
    """Stops within radius_km of the route, with their snapped position on it.

    Uses the fast downsampled-vertex scan to find nearby stops. For each
    qualifying stop the coordinates of its nearest route vertex are returned
    as (snapped_lon, snapped_lat) so the map marker can be placed directly ON
    the route line rather than at the real GPS position of the station.

    Returns a list of
        (stop, off_route_km, along_route_km, snapped_lon, snapped_lat)
    sorted by distance travelled along the route (start → destination).
    """
    cum = cumulative_km(route_coords)
    lons, lats, alongs = downsample_route(route_coords, cum, max(radius_km, 1.0))
    min_lon, min_lat, max_lon, max_lat = bounding_box(route_coords, radius_km)
    n = len(lons)
    found = []
    for stop in stops:
        lon, lat = stop.geocoded_lon, stop.geocoded_lat
        if lon is None or lat is None:
            continue
        if not (min_lon <= lon <= max_lon and min_lat <= lat <= max_lat):
            continue
        best_d, best_i = None, 0
        for i in range(n):
            d = haversine_km(lon, lat, lons[i], lats[i])
            if best_d is None or d < best_d:
                best_d, best_i = d, i
        if best_d <= radius_km:
            # Snap the marker to the nearest vertex on the route
            found.append((stop, best_d, alongs[best_i], lons[best_i], lats[best_i]))
    found.sort(key=lambda t: t[2])
    return found



def plan_fuel_stops(
    along_stops,
    total_km,
    range_miles=DEFAULT_RANGE_MILES,
    mpg=DEFAULT_TRUCK_MPG,
    price_attr="average_retail_price",
):
    """Choose refuel stops so no leg exceeds the vehicle range, minimising price.

    `along_stops` is the output of `stops_along_route`. The truck departs on a
    full tank; whenever the destination is farther than one tank away it must
    refuel before the range runs out, so we pick the cheapest priced stop within
    reach and continue from there. Fuel for each leg is paid at the price of the
    stop that begins it; total gallons = total miles / mpg.

    Returns a dict:
      feasible          - False if a >range gap has no stop to refuel at
      fuel_stops        - chosen stops, in travel order, each a dict
      total_gallons     - gallons consumed over the whole trip
      total_cost        - total money spent on fuel
    """
    range_km = range_miles * KM_PER_MILE
    total_miles = total_km / KM_PER_MILE

    # Only stops with a known price (in the chosen column) can serve as fuel-ups.
    priced = [
        {
            "stop": s,
            "off_km": off,
            "along_km": along,
            "price": float(getattr(s, price_attr)),
            "snapped_lon": slon,
            "snapped_lat": slat,
        }
        for (s, off, along, slon, slat) in along_stops
        if getattr(s, price_attr) is not None
    ]
    priced.sort(key=lambda c: c["along_km"])

    selected = []
    pos = 0.0
    feasible = True
    while total_km - pos > range_km:
        window = [c for c in priced if pos < c["along_km"] <= pos + range_km]
        if not window:
            feasible = False
            break
        choice = min(window, key=lambda c: c["price"])
        selected.append(choice)
        pos = choice["along_km"]

    # Fuel left on arrival. The range-driven loop above is the set of *physical*
    # refuels (the one-tank "buy once" case below is only a pricing fiction, not
    # a real stop). Assuming the truck departs full and tops up to full at each
    # stop, arrival fuel = tank capacity minus the burn over the final leg.
    tank_capacity_gallons = range_miles / mpg
    if feasible:
        last_fill_km = selected[-1]["along_km"] if selected else 0.0
        final_leg_miles = (total_km - last_fill_km) / KM_PER_MILE
        arrival_gallons = max(0.0, tank_capacity_gallons - final_leg_miles / mpg)
    else:
        arrival_gallons = None  # trip can't be completed as planned

    # Trip fits in one tank: still buy fuel once, at the cheapest stop on route.
    if feasible and not selected and priced:
        selected.append(min(priced, key=lambda c: c["price"]))

    # Cost: each chosen stop's price covers the leg up to the next stop.
    total_cost = None
    if selected:
        total_cost = 0.0
        prev_km = 0.0
        boundaries = [c["along_km"] for c in selected[1:]] + [total_km]
        for c, seg_end in zip(selected, boundaries):
            seg_miles = (seg_end - prev_km) / KM_PER_MILE
            total_cost += (seg_miles / mpg) * c["price"]
            prev_km = seg_end

    for i, c in enumerate(selected, start=1):
        c["order"] = i

    return {
        "feasible": feasible,
        "fuel_stops": selected,
        "total_gallons": total_miles / mpg,
        "total_cost": total_cost,
        "arrival_gallons": arrival_gallons,
        "tank_capacity_gallons": tank_capacity_gallons,
    }
