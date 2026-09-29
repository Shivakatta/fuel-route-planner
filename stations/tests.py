import csv
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.test import Client, TestCase

from stations.models import FuelStation
from stations.route_planner import build_fuel_plan


class ImportFuelPricesTests(TestCase):
	def test_import_is_repeatable_and_keeps_missing_coordinates_null(self):
		with tempfile.TemporaryDirectory() as directory:
			csv_path = Path(directory) / "fuel_prices.csv"
			with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
				writer = csv.DictWriter(
					csv_file,
					fieldnames=["station_id", "name", "address", "city", "state", "price", "lat", "lon"],
				)
				writer.writeheader()
				writer.writerow({
					"station_id": "7",
					"name": "Test Fuel",
					"address": "I-44, EXIT 283",
					"city": "Big Cabin",
					"state": "OK",
					"price": "3.007",
					"lat": "",
					"lon": "",
				})

			call_command("import_fuel_prices", str(csv_path), verbosity=0)
			call_command("import_fuel_prices", str(csv_path), verbosity=0)

			station = FuelStation.objects.get(station_id=7)
			self.assertEqual(station.price, Decimal("3.007"))
			self.assertIsNone(station.latitude)
			self.assertIsNone(station.longitude)
			station.latitude = Decimal("36.000000")
			station.longitude = Decimal("-95.000000")
			station.location_source = "census_place_centroid"
			station.save()
			call_command("import_fuel_prices", str(csv_path), verbosity=0)
			station.refresh_from_db()
			self.assertEqual(station.latitude, Decimal("36.000000"))
			self.assertEqual(station.longitude, Decimal("-95.000000"))
			self.assertEqual(station.location_source, "census_place_centroid")

		self.assertEqual(FuelStation.objects.count(), 1)
		station = FuelStation.objects.get(station_id=7)
		self.assertEqual(station.price, Decimal("3.007"))


class FuelPlanTests(TestCase):
	def test_long_route_selects_stops_within_vehicle_range(self):
		stations = [
			self._station(1, 0.1, 3.00),
			self._station(2, 4.9, 2.00),
			self._station(3, 9.8, 3.00),
		]

		result = build_fuel_plan([[0, 0], [10, 0]], 1000, stations)

		self.assertEqual([stop["station_id"] for stop in result["stops"]], [2, 3])
		self.assertEqual(result["estimated_gallons"], 100.0)
		self.assertEqual(result["estimated_total_cost_usd"], 251.0)
		route_positions = [0] + [stop["route_distance_miles"] for stop in result["stops"]] + [1000]
		self.assertTrue(all(
			following - previous <= 500
			for previous, following in zip(route_positions, route_positions[1:])
		))

	@staticmethod
	def _station(station_id, longitude, price):
		return {
			"station_id": station_id,
			"name": f"Station {station_id}",
			"address": "",
			"city": "Test City",
			"state": "OK",
			"price": Decimal(str(price)),
			"latitude": Decimal("0"),
			"longitude": Decimal(str(longitude)),
			"location_source": "test",
		}


class RouteApiTests(TestCase):
	def test_accepts_json_and_returns_route_plan(self):
		with patch("stations.views.plan_route", return_value={"route": {"distance_miles": 25}}) as planner:
			response = Client().post(
				"/api/route/",
				data='{"start":"Tulsa, OK","finish":"Oklahoma City, OK"}',
				content_type="application/json",
			)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json(), {"route": {"distance_miles": 25}})
		planner.assert_called_once_with("Tulsa, OK", "Oklahoma City, OK")

	def test_rejects_invalid_json(self):
		response = Client().post("/api/route/", data="{", content_type="application/json")

		self.assertEqual(response.status_code, 400)

	def test_rejects_missing_start_location(self):
		response = Client().post(
			"/api/route/",
			data='{"finish":"Oklahoma City, OK"}',
			content_type="application/json",
		)

		self.assertEqual(response.status_code, 400)
		self.assertIn("start", response.json()["error"])
