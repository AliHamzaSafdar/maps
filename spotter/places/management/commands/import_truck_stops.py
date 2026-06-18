import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from places.models import Place

# Default location of the CSV relative to the repo root (parent of manage.py).
DEFAULT_CSV = (
    Path(__file__).resolve().parents[4] / "truck_stops_aws_geocoded_combined.csv"
)


def _str(value):
    return (value or "").strip()


def _decimal(value):
    value = _str(value)
    if not value:
        return None
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError):
        return None


def _float(value):
    value = _str(value)
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


class Command(BaseCommand):
    help = "Import truck stops from truck_stops_aws_geocoded_combined.csv into the Place model."

    def add_arguments(self, parser):
        parser.add_argument(
            "csv_path",
            nargs="?",
            default=str(DEFAULT_CSV),
            help="Path to the CSV file (defaults to the combined geocoded CSV at the repo root).",
        )
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Delete all existing Place rows before importing.",
        )

    def handle(self, *args, **options):
        csv_path = Path(options["csv_path"])
        if not csv_path.exists():
            raise CommandError(f"CSV file not found: {csv_path}")

        with transaction.atomic():
            if options["clear"]:
                deleted, _ = Place.objects.all().delete()
                self.stdout.write(self.style.WARNING(f"Cleared {deleted} existing rows."))

            created_count = 0
            updated_count = 0
            skipped = 0
            with csv_path.open(newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    opis_id = _str(row.get("OPIS Truckstop ID"))
                    if not opis_id:
                        skipped += 1
                        continue
                    _, created = Place.objects.update_or_create(
                        opis_truckstop_id=opis_id,
                        defaults={
                            "name": _str(row.get("Truckstop_Name")),
                            "address": _str(row.get("Address")),
                            "city": _str(row.get("City")),
                            "state": _str(row.get("State")),
                            "rack_id": _str(row.get("Rack_ID")),
                            "average_retail_price": _decimal(row.get("Average_Retail_Price")),
                            "highest_price": _decimal(row.get("Highest_Price")),
                            "city_lat": _float(row.get("city_lat")),
                            "city_lon": _float(row.get("city_lon")),
                            "geocoded_lat": _float(row.get("Geocoded_Lat")),
                            "geocoded_lon": _float(row.get("Geocoded_Lon")),
                            "matched_address": _str(row.get("Matched_Address")),
                            "geocode_status": _str(row.get("Geocode_Status")),
                        },
                    )
                    if created:
                        created_count += 1
                    else:
                        updated_count += 1

        msg = f"Done: {created_count} created, {updated_count} updated"
        if skipped:
            msg += f", {skipped} skipped (blank OPIS ID)"
        self.stdout.write(self.style.SUCCESS(f"{msg} from {csv_path}."))
