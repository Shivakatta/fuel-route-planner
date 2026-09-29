import csv
import io
import re
import unicodedata
import urllib.request
import zipfile
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from stations.models import FuelStation


GAZETTEER_URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
    "2026_Gazetteer/2026_Gaz_place_national.zip"
)
PLACE_SUFFIXES = (" city", " town", " village", " borough", " cdp", " municipality")


class Command(BaseCommand):
    help = "Add approximate city/state coordinates using the US Census Gazetteer."

    def add_arguments(self, parser):
        parser.add_argument("--url", default=GAZETTEER_URL, help="Census Gazetteer places ZIP URL")

    @transaction.atomic
    def handle(self, *args, **options):
        try:
            request = urllib.request.Request(
                options["url"], headers={"User-Agent": "FuelRoutePlanner/1.0"}
            )
            with urllib.request.urlopen(request, timeout=60) as response:
                archive = zipfile.ZipFile(io.BytesIO(response.read()))
        except (OSError, zipfile.BadZipFile) as exc:
            raise CommandError(f"Could not download or open the Census Gazetteer: {exc}") from exc

        place_file = next(
            (name for name in archive.namelist() if name.lower().endswith("place_national.txt")),
            None,
        )
        if not place_file:
            raise CommandError("The Gazetteer archive does not contain a national places file.")

        try:
            text = archive.read(place_file).decode("utf-8-sig")
            place_coordinates = self._parse_places(text)
        except (UnicodeDecodeError, csv.Error, KeyError, InvalidOperation) as exc:
            raise CommandError(f"Could not parse the Census places file: {exc}") from exc

        updated = []
        unmatched = set()
        for station in FuelStation.objects.filter(latitude__isnull=True, longitude__isnull=True).iterator():
            coordinates = place_coordinates.get((station.state, self._normalize(station.city)))
            if coordinates is None:
                unmatched.add((station.city, station.state))
                continue
            station.latitude, station.longitude = coordinates
            station.location_source = "census_place_centroid"
            updated.append(station)

        FuelStation.objects.bulk_update(
            updated, ["latitude", "longitude", "location_source"], batch_size=1000
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Added approximate coordinates to {len(updated)} stations; "
                f"{len(unmatched)} city/state pairs did not match."
            )
        )

    @classmethod
    def _parse_places(cls, text):
        coordinates = {}
        reader = csv.DictReader(io.StringIO(text), delimiter="|")
        for row in reader:
            state = row["USPS"].strip().upper()
            name = cls._normalize(row["NAME"])
            try:
                point = (Decimal(row["INTPTLAT"]), Decimal(row["INTPTLONG"]))
            except (KeyError, InvalidOperation):
                continue
            coordinates.setdefault((state, name), point)
            for suffix in PLACE_SUFFIXES:
                if name.endswith(suffix):
                    coordinates.setdefault((state, name[: -len(suffix)]), point)
                    break
        return coordinates

    @staticmethod
    def _normalize(value):
        value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
        return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()