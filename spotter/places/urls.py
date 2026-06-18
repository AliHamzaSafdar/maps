from django.urls import path

from . import views

app_name = "places"

urlpatterns = [
    path("", views.planner, name="planner"),
    path("suggest/", views.suggest_view, name="suggest"),
]
