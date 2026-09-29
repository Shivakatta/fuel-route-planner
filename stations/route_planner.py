import hashlib
import json
import math
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.core.cache import cache

from stations.models import FuelStation


MAX_RANGE_MILES = 500.0
MILES_PER_GALLON = 10.0
ROUTE_CORRIDOR_MILES = 20.0
START_PRICE_LOOKAHEAD_MILES = 25.0
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving"
_geocoder_lock = threading.Lock()
_last_geocoder_request = 0.0


class RoutePlanningError(Exception):
    def __init__(self, message, code="route_planning_failed", status_code=502):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def plan_route(start, finish):
    origin = _geocode_us_place(start)
    destination = _geocode_us_place(finish)
    route = _get_driving_route(origin, destination)

    stations = list(
        FuelStation.objects.filter(latitude__isnull=False, longitude__isnull=False)
        .exclude(price_outlier=True)
        .values(
            "station_id", "name", "address", "city", "state", "price",
            "latitude", "longitude", "location_source",
        )
    )
    fuel_plan = build_fuel_plan(route["geometry"]["coordinates"], route["distance_miles"], stations)
    return {
        "origin": {"query": start, "resolved_name": origin["name"]},
        "destination": {"query": finish, "resolved_name": destination["name"]},
        "route": {
            "distance_miles": round(route["distance_miles"], 1),
            "duration_minutes": round(route["duration_minutes"], 1),
            "geometry": route["geometry"],
        },
        "fuel_plan": fuel_plan,
    }


def build_fuel_plan(route_coordinates, route_distance_miles, stations):
    if not stations:
        raise RoutePlanningError(
            "No stations have coordinates. Run 'python manage.py geocode_stations' first.",
            code="station_coordinates_missing",
            status_code=503,
        )
    if route_distance_miles <= 0 or len(route_coordinates) < 2:
        raise RoutePlanningError("The routing service returned an invalid route.", code="invalid_route")

    route_measurements = _measure_route(route_coordinates)
    measured_distance = route_measurements[-1]["distance"]
    scale = route_distance_miles / measured_distance
    best_by_mile = {}

    for station in stations:
        point = _nearest_route_position(
            float(station["longitude"]), float(station["latitude"]), route_measurements
        )
        if point["deviation"] > ROUTE_CORRIDOR_MILES:
            continue
        position = min(route_distance_miles, max(0.0, point["distance"] * scale))
        if position < 1 or position >= route_distance_miles:
            continue
        bucket = int(position)
        candidate = {
            **station,
            "route_distance_miles": position,
            "route_deviation_miles": point["deviation"],
            "price": float(station["price"]),
        }
        current = best_by_mile.get(bucket)
        if current is None or (candidate["price"], candidate["station_id"]) < (
            current["price"], current["station_id"]
        ):
            best_by_mile[bucket] = candidate

    candidates = sorted(best_by_mile.values(), key=lambda station: station["route_distance_miles"])
    start_sources = [
        station for station in candidates
        if station["route_distance_miles"] <= START_PRICE_LOOKAHEAD_MILES
    ]
    if not start_sources:
        raise RoutePlanningError(
            "No fuel station was found near the start of the route.",
            code="no_stations_near_start",
            status_code=422,
        )

    start_source = min(start_sources, key=lambda station: (station["price"], station["station_id"]))
    nodes = [{"position": 0.0, "price": start_source["price"], "station": None}]
    nodes.extend(
        {
            "position": station["route_distance_miles"],
            "price": station["price"],
            "station": station,
        }
        for station in candidates
        if station["route_distance_miles"] > START_PRICE_LOOKAHEAD_MILES
    )
    nodes.sort(key=lambda node: node["position"])
    nodes.append({"position": route_distance_miles, "price": None, "station": None})

    costs = [math.inf] * len(nodes)
    previous = [None] * len(nodes)
    costs[0] = 0.0
    for end_index in range(1, len(nodes)):
        end_position = nodes[end_index]["position"]
        for start_index in range(end_index - 1, -1, -1):
            distance = end_position - nodes[start_index]["position"]
            if distance > MAX_RANGE_MILES:
                break
            if costs[start_index] == math.inf:
                continue
            fuel_cost = distance / MILES_PER_GALLON * nodes[start_index]["price"]
            candidate_cost = costs[start_index] + fuel_cost
            if candidate_cost < costs[end_index]:
                costs[end_index] = candidate_cost
                previous[end_index] = start_index

    destination_index = len(nodes) - 1
    if costs[destination_index] == math.inf:
        raise RoutePlanningError(
            "Fuel stations are too far apart to complete this route within a 500-mile range.",
            code="route_exceeds_vehicle_range",
            status_code=422,
        )

    selected = []
    current_index = destination_index
    while current_index:
        current_index = previous[current_index]
        if current_index is None:
            raise RoutePlanningError("Could not construct a valid fuel plan.")
        station = nodes[current_index]["station"]
        if station is not None:
            selected.append(station)
    selected.reverse()

    return {
        "vehicle_range_miles": int(MAX_RANGE_MILES),
        "miles_per_gallon": int(MILES_PER_GALLON),
        "estimated_gallons": round(route_distance_miles / MILES_PER_GALLON, 2),
        "estimated_total_cost_usd": round(costs[destination_index], 2),
        "starting_fuel_price_source": _serialize_station(start_source),
        "stops": [
            {
                **_serialize_station(station),
                "route_distance_miles": round(station["route_distance_miles"], 1),
                "route_deviation_miles": round(station["route_deviation_miles"], 1),
            }
            for station in selected
        ],
        "coordinate_accuracy": (
            "Station coordinates are Census city/state representative points, not verified forecourt locations."
        ),
        "cost_assumptions": (
            "The vehicle is refueled at the route origin using the price of the selected nearby station; "
            "each leg buys only the fuel needed to reach the next stop or destination."
        ),
    }


def _serialize_station(station):
    return {
        "station_id": station["station_id"],
        "name": station["name"],
        "address": station["address"],
        "city": station["city"],
        "state": station["state"],
        "price_per_gallon_usd": round(station["price"], 3),
        "latitude": float(station["latitude"]),
        "longitude": float(station["longitude"]),
        "location_source": station["location_source"],
    }


def _measure_route(coordinates):
    measurements = [{"distance": 0.0}]
    total = 0.0
    for index in range(1, len(coordinates)):
        lon1, lat1 = coordinates[index - 1]
        lon2, lat2 = coordinates[index]
        total += _haversine_miles(lon1, lat1, lon2, lat2)
        measurements.append({
            "distance": total,
            "start": (lon1, lat1),
            "end": (lon2, lat2),
        })
    return measurements


def _nearest_route_position(longitude, latitude, measurements):
    nearest = {"deviation": math.inf, "distance": 0.0}
    for index in range(1, len(measurements)):
        segment = measurements[index]
        lon1, lat1 = segment["start"]
        lon2, lat2 = segment["end"]
        mean_latitude = math.radians((lat1 + lat2 + latitude) / 3)
        x_scale = math.cos(mean_latitude)
        dx = (lon2 - lon1) * x_scale
        dy = lat2 - lat1
        px = (longitude - lon1) * x_scale
        py = latitude - lat1
        length_squared = dx * dx + dy * dy
        fraction = min(1.0, max(0.0, (px * dx + py * dy) / length_squared)) if length_squared else 0.0
        deviation = math.hypot(px - fraction * dx, py - fraction * dy) * 69.0
        if deviation < nearest["deviation"]:
            segment_miles = segment["distance"] - measurements[index - 1]["distance"]
            nearest = {
                "deviation": deviation,
                "distance": measurements[index - 1]["distance"] + fraction * segment_miles,
            }
    return nearest


def _haversine_miles(lon1, lat1, lon2, lat2):
    radius_miles = 3958.7613
    lat1, lat2 = math.radians(lat1), math.radians(lat2)
    delta_lat = lat2 - lat1
    delta_lon = math.radians(lon2 - lon1)
    value = math.sin(delta_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    return 2 * radius_miles * math.asin(math.sqrt(min(1.0, value)))


def _geocode_us_place(place):
    key = "fuel-route-geocode:" + hashlib.sha256(place.casefold().encode("utf-8")).hexdigest()
    cached = cache.get(key)
    if cached:
        return cached

    params = urlencode({"q": place, "format": "jsonv2", "limit": 1, "countrycodes": "us"})
    url = f"{NOMINATIM_URL}?{params}"
    with _geocoder_lock:
        global _last_geocoder_request
        delay = 1.0 - (time.monotonic() - _last_geocoder_request)
        if delay > 0:
            time.sleep(delay)
        _last_geocoder_request = time.monotonic()
        payload = _request_json(url)

    if not payload:
        raise RoutePlanningError(
            f"Could not find a US location matching '{place}'.",
            code="location_not_found",
            status_code=422,
        )
    result = {"latitude": float(payload[0]["lat"]), "longitude": float(payload[0]["lon"]), "name": payload[0]["display_name"]}
    cache.set(key, result, 60 * 60 * 24)
    return result


def _get_driving_route(origin, destination):
    coordinates = (
        f"{origin['longitude']},{origin['latitude']};"
        f"{destination['longitude']},{destination['latitude']}"
    )
    params = urlencode({"overview": "full", "geometries": "geojson", "steps": "false", "alternatives": "false"})
    route_data = _request_json(f"{OSRM_URL}/{coordinates}?{params}")
    if route_data.get("code") != "Ok" or not route_data.get("routes"):
        raise RoutePlanningError("The routing service could not find a driving route.", code="route_not_found", status_code=422)
    route = route_data["routes"][0]
    return {
        "distance_miles": route["distance"] / 1609.344,
        "duration_minutes": route["duration"] / 60,
        "geometry": route["geometry"],
    }


def _request_json(url):
    request = Request(url, headers={"User-Agent": "FuelRoutePlanner/1.0"})
    try:
        with urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RoutePlanningError(f"A map provider returned HTTP {exc.code}.") from exc
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise RoutePlanningError("A map provider is temporarily unavailable.") from exc