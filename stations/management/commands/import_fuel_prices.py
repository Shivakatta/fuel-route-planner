import csv
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from stations.models import FuelStation


class Command(BaseCommand):
    help = "Import fuel prices from a CSV file into the station database."

    def add_arguments(self, parser):
        parser.add_argument("csv_path", help="Path to fuel_prices_clean.csv")

    @transaction.atomic
    def handle(self, *args, **options):
        csv_path = options["csv_path"]
        stations = []

        try:
            with open(csv_path, newline="", encoding="utf-8-sig") as csv_file:
                reader = csv.DictReader(csv_file)
                required = {"station_id", "name", "city", "state", "price"}
                if not reader.fieldnames or not required.issubset(reader.fieldnames):
                    raise CommandError("CSV is missing required columns: " + ", ".join(sorted(required)))

                for row in reader:
                    try:
                        stations.append(
                            FuelStation(
                                station_id=int(row["station_id"]),
                                name=row["name"].strip(),
                                address=(row.get("address") or "").strip(),
                                city=row["city"].strip(),
                                state=row["state"].strip().upper(),
                                rack_id=int(row["rack_id"]) if row.get("rack_id") else None,
                                price=Decimal(row["price"]),
                                price_min=self._decimal_or_none(row.get("price_min")),
                                price_max=self._decimal_or_none(row.get("price_max")),
                                source_rows=int(row.get("source_rows") or 1),
                                price_outlier=(row.get("price_outlier") or "").strip().lower() == "true",
                                latitude=self._decimal_or_none(row.get("lat")),
                                longitude=self._decimal_or_none(row.get("lon")),
                                location_source=(
                                    "csv" if row.get("lat") and row.get("lon") else ""
                                ),
                            )
                        )
                    except (ValueError, InvalidOperation, KeyError) as exc:
                        raise CommandError(
                            f"Invalid station row {reader.line_num}: {exc}"
                        ) from exc
        except OSError as exc:
            raise CommandError(f"Could not read {csv_path}: {exc}") from exc

        if not stations:
            raise CommandError("The CSV contains no station rows.")

        existing_locations = {
            station.station_id: station
            for station in FuelStation.objects.filter(
                station_id__in=[station.station_id for station in stations]
            ).only("station_id", "latitude", "longitude", "location_source")
        }
        for station in stations:
            existing = existing_locations.get(station.station_id)
            if existing and (station.latitude is None or station.longitude is None):
                station.latitude = existing.latitude
                station.longitude = existing.longitude
                station.location_source = existing.location_source

        FuelStation.objects.bulk_create(
            stations,
            batch_size=1000,
            update_conflicts=True,
            unique_fields=["station_id"],
            update_fields=[
                "name", "address", "city", "state", "rack_id", "price", "price_min",
                "price_max", "source_rows", "price_outlier", "latitude", "longitude",
                "location_source",
            ],
        )
        missing_coordinates = sum(
            station.latitude is None or station.longitude is None for station in stations
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Imported {len(stations)} stations; {missing_coordinates} have no coordinates."
            )
        )

    @staticmethod
    def _decimal_or_none(value):
        return Decimal(value) if value and value.strip() else None