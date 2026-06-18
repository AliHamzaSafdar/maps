from django.contrib import admin

from .models import Place


@admin.register(Place)
class PlaceAdmin(admin.ModelAdmin):
    list_display = ("name", "city", "state", "average_retail_price", "geocode_status")
    list_filter = ("state", "geocode_status")
    search_fields = ("name", "city", "state", "address", "opis_truckstop_id")
