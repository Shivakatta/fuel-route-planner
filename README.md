# Fuel Route Planner API

Django API that returns a US driving route and fuel plan using the supplied station-price CSV. The response includes a GeoJSON route line and station coordinates that a map client can render.

## Setup

Use Python 3.12 or newer. From the project directory, activate the virtual environment if needed, then run:

```powershell
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py import_fuel_prices "C:\Users\SHIVA\Downloads\fuel_prices_clean.csv"
python manage.py geocode_stations
python manage.py runserver 127.0.0.1:8000
```

The importer is safe to run again after updating the CSV; it preserves previously enriched coordinates when the CSV has blank coordinates. `geocode_stations` downloads the official US Census national places Gazetteer once and matches each station's city/state to an approximate representative point. It makes no request per station.

## Endpoint

`POST http://127.0.0.1:8000/api/route/`

Content-Type: `application/json`

```json
{
  "start": "Tulsa, Oklahoma, USA",
  "finish": "Oklahoma City, Oklahoma, USA"
}
```

The success response contains `route.distance_miles`, `route.duration_minutes`, and `route.geometry` (GeoJSON LineString), plus `fuel_plan.stops`. Each stop has the station's price, coordinates, route position, and coordinate source. `fuel_plan.estimated_total_cost_usd` uses 10 MPG and includes the estimated cost of the fuel consumed from the route origin through the destination.

### Providers and assumptions

- Nominatim (OpenStreetMap) resolves each endpoint with a US-only search. Successful place lookups are cached for 24 hours; repeated routes reuse them.
- OSRM's public demo server calculates the driving route. A cache miss uses at most three provider requests: two geocodes and one route request. These public demo services are best-effort and have usage policies, so production use should use hosted providers with an appropriate SLA and credentials.
- The source CSV contains no station coordinates. Census coordinates identify representative city/state points, not exact station entrances; the response exposes `location_source` and warns about this approximation. Stops are stations within 20 miles of the route corridor.
- Since no starting fuel quantity is specified, the planner assumes the vehicle buys its trip fuel at the route origin, pricing that initial fuel with the cheapest station found within the first 25 route miles. It then minimizes estimated fuel cost for legs that may not exceed the 500-mile range. The response exposes this estimate and assumption explicitly.
- Prices are used as provided by the CSV; outlier rows are excluded. Distances and cost are estimates, not turn-by-turn station access guarantees.

## Postman quick check

Create a POST request to `http://127.0.0.1:8000/api/route/`, select **Body > raw > JSON**, and paste the example payload above. Invalid JSON or missing locations return HTTP 400. The returned GeoJSON `route.geometry` can be drawn as a line and each `fuel_plan.stops` coordinate as a marker by any GeoJSON-capable map viewer.

## Tests

```powershell
python manage.py test stations
python manage.py check
python manage.py makemigrations --check --dry-run
```