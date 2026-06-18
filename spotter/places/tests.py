from types import SimpleNamespace

from django.test import SimpleTestCase

from places import geo
from places.geo import KM_PER_MILE


def stop(lon, lat, price=None):
    return SimpleNamespace(geocoded_lon=lon, geocoded_lat=lat, average_retail_price=price)


class HaversineTests(SimpleTestCase):
    def test_zero_distance(self):
        self.assertAlmostEqual(geo.haversine_km(-95, 36, -95, 36), 0.0, places=6)

    def test_known_distance(self):
        # ~1 degree of latitude is ~111 km.
        d = geo.haversine_km(0, 0, 0, 1)
        self.assertAlmostEqual(d, 111.19, delta=0.5)


class StopsAlongRouteTests(SimpleTestCase):
    def setUp(self):
        # A straight route along the equator from lon 0 to lon 2.
        self.route = [[0.0, 0.0], [1.0, 0.0], [2.0, 0.0]]

    def test_includes_on_route_stop_with_along_distance(self):
        result = geo.stops_along_route([stop(1.0, 0.0)], self.route, radius_km=8)
        self.assertEqual(len(result), 1)
        _, off_km, along_km, _slon, _slat = result[0]
        self.assertAlmostEqual(off_km, 0.0, places=3)
        self.assertAlmostEqual(along_km, geo.haversine_km(0, 0, 1, 0), delta=0.5)

    def test_excludes_far_stop(self):
        result = geo.stops_along_route([stop(1.0, 1.0)], self.route, radius_km=8)
        self.assertEqual(result, [])

    def test_sorted_by_along_distance(self):
        a = stop(2.0, 0.0)
        b = stop(0.0, 0.0)
        result = geo.stops_along_route([a, b], self.route, radius_km=8)
        self.assertEqual([s for s, _, _, _, _ in result], [b, a])

    def test_returns_real_station_coords(self):
        # Markers are no longer snapped to the route — a stop slightly off the
        # line must report its OWN coords, since the route is later detoured to it.
        result = geo.stops_along_route([stop(1.0, 0.03)], self.route, radius_km=8)
        self.assertEqual(len(result), 1)
        _, off_km, _, lon, lat = result[0]
        self.assertAlmostEqual(lon, 1.0, places=6)
        self.assertAlmostEqual(lat, 0.03, places=6)
        self.assertGreater(off_km, 0.0)  # genuinely off the route line

class FuelStopPlanTests(SimpleTestCase):
    def along(self, *entries):
        """Build stops_along_route-style 5-tuples: (stop, off_km, along_km, slon, slat)."""
        return [(stop(0, 0, price), 1.0, along_km, 0.0, 0.0) for price, along_km in entries]

    def test_one_tank_trip_picks_single_cheapest(self):
        # 300-mile trip, 500-mile range -> no mandatory stop, one fill at cheapest.
        total_km = 300 * KM_PER_MILE
        plan = geo.plan_fuel_stops(
            self.along((4.0, 100 * KM_PER_MILE), (3.0, 200 * KM_PER_MILE)),
            total_km, range_miles=500, mpg=10,
        )
        self.assertTrue(plan["feasible"])
        self.assertEqual(len(plan["fuel_stops"]), 1)
        self.assertEqual(plan["fuel_stops"][0]["price"], 3.0)
        self.assertAlmostEqual(plan["total_gallons"], 30.0, places=3)
        self.assertAlmostEqual(plan["total_cost"], 30.0 * 3.0, places=2)

    def test_long_trip_requires_multiple_stops(self):
        # 900-mile trip, 400-mile range -> must refuel along the way.
        total_km = 900 * KM_PER_MILE
        entries = [(3.0 + (i % 3) * 0.1, m * KM_PER_MILE) for i, m in enumerate(range(100, 900, 100))]
        plan = geo.plan_fuel_stops(entries_to_along(entries), total_km, range_miles=400, mpg=10)
        self.assertTrue(plan["feasible"])
        # Each leg (start->1, 1->2, ..., last->dest) must be <= range.
        positions = [0.0] + [s["along_km"] for s in plan["fuel_stops"]] + [total_km]
        for a, b in zip(positions, positions[1:]):
            self.assertLessEqual((b - a) / KM_PER_MILE, 400 + 1e-6)
        self.assertGreaterEqual(len(plan["fuel_stops"]), 2)

    def test_infeasible_when_gap_exceeds_range(self):
        # Only stop is at mile 50; destination at mile 900 with 400-mile range.
        total_km = 900 * KM_PER_MILE
        plan = geo.plan_fuel_stops(
            self.along((3.0, 50 * KM_PER_MILE)), total_km, range_miles=400, mpg=10
        )
        self.assertFalse(plan["feasible"])

    def test_total_gallons_uses_mpg(self):
        total_km = 500 * KM_PER_MILE
        plan = geo.plan_fuel_stops(
            self.along((3.0, 100 * KM_PER_MILE)), total_km, range_miles=500, mpg=10
        )
        self.assertAlmostEqual(plan["total_gallons"], 50.0, places=3)


class MetricsFromLegsTests(SimpleTestCase):
    def test_fill_to_full_cost_and_arrival(self):
        # start -> stop1 -> end. Legs: 300 mi to the stop, 100 mi to the end.
        # 10 mpg, 500 mi range -> capacity 50 gal.
        # Refill at stop1 = 300 mi burn = 30 gal @ $3 = $90.
        # Arrival = 50 - 100/10 = 40 gal.
        legs_km = [300 * KM_PER_MILE, 100 * KM_PER_MILE]
        m = geo.metrics_from_legs(legs_km, [3.0], range_miles=500, mpg=10)
        self.assertTrue(m["feasible"])
        self.assertAlmostEqual(m["total_cost"], 90.0, places=2)
        self.assertAlmostEqual(m["arrival_gallons"], 40.0, places=2)
        self.assertAlmostEqual(m["total_gallons"], 40.0, places=2)
        self.assertEqual(m["tank_capacity_gallons"], 50.0)

    def test_leg_over_range_is_infeasible(self):
        # A single leg longer than the tank range can't be driven.
        legs_km = [600 * KM_PER_MILE, 50 * KM_PER_MILE]
        m = geo.metrics_from_legs(legs_km, [3.0], range_miles=500, mpg=10)
        self.assertFalse(m["feasible"])
        self.assertIsNone(m["arrival_gallons"])


def entries_to_along(entries):
    return [(SimpleNamespace(geocoded_lon=0, geocoded_lat=0, average_retail_price=p), 1.0, km, 0.0, 0.0) for p, km in entries]

