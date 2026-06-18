from django.db import models


class Place(models.Model):
    """A truck stop / fuel location with pricing and geocoded coordinates."""

    opis_truckstop_id = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=128, blank=True)
    state = models.CharField(max_length=64, blank=True)
    rack_id = models.CharField(max_length=64, blank=True)

    average_retail_price = models.DecimalField(
        max_digits=8, decimal_places=3, null=True, blank=True
    )
    highest_price = models.DecimalField(
        max_digits=8, decimal_places=3, null=True, blank=True
    )

    city_lat = models.FloatField(null=True, blank=True)
    city_lon = models.FloatField(null=True, blank=True)
    geocoded_lat = models.FloatField(null=True, blank=True)
    geocoded_lon = models.FloatField(null=True, blank=True)

    matched_address = models.CharField(max_length=512, blank=True)
    geocode_status = models.CharField(max_length=64, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.city}, {self.state})"
