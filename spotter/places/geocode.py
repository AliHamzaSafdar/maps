"""Geocode a place name to (lon, lat).

Free path uses OpenStreetMap's Nominatim; AWS path uses AWS Location's
geo-places. Both return (lon, lat) or None when nothing is found.
"""

import json
import urllib.parse
import urllib.request

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "GMAOS-route-planner/1.0 (ali.hamza@thepattern.app)"


def geocode_free(query):
    params = urllib.parse.urlencode(
        {"q": query, "format": "json", "limit": 1, "countrycodes": "us"}
    )
    req = urllib.request.Request(
        f"{NOMINATIM_URL}?{params}", headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    if not data:
        return None
    return float(data[0]["lon"]), float(data[0]["lat"])


def geocode_aws(query):
    import boto3

    # Filter to US so AWS never returns a foreign match (mirrors the free
    # path's countrycodes=us); avoids border-region false positives.
    resp = boto3.client("geo-places").geocode(
        QueryText=query,
        MaxResults=1,
        Filter={"IncludeCountries": ["USA"]},
    )
    items = resp.get("ResultItems", [])
    if not items:
        return None
    pos = items[0].get("Position")  # [lon, lat]
    if not pos:
        return None
    return float(pos[0]), float(pos[1])


def geocode(query, provider="free"):
    """Resolve a place name to (lon, lat) using the chosen provider."""
    if provider == "aws":
        return geocode_aws(query)
    return geocode_free(query)


def suggest_free(query, limit=5):
    params = urllib.parse.urlencode(
        {"q": query, "format": "json", "limit": limit, "countrycodes": "us"}
    )
    req = urllib.request.Request(
        f"{NOMINATIM_URL}?{params}", headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    return [
        {"label": item["display_name"], "lon": float(item["lon"]), "lat": float(item["lat"])}
        for item in data
    ]


def suggest_aws(query, limit=5):
    import boto3

    resp = boto3.client("geo-places").geocode(
        QueryText=query,
        MaxResults=limit,
        Filter={"IncludeCountries": ["USA"]},
    )
    out = []
    for item in resp.get("ResultItems", []):
        pos = item.get("Position")
        if not pos:
            continue
        label = item.get("Address", {}).get("Label") or item.get("Title", query)
        out.append({"label": label, "lon": float(pos[0]), "lat": float(pos[1])})
    return out


def suggest(query, provider="free", limit=5):
    """Return up to `limit` location candidates [{label, lon, lat}, ...]."""
    if provider == "aws":
        return suggest_aws(query, limit)
    return suggest_free(query, limit)


def reverse_country_free(lon, lat):
    # zoom=3 keeps the lookup at country level — smaller/faster response.
    params = urllib.parse.urlencode(
        {"lat": lat, "lon": lon, "format": "json", "zoom": 3}
    )
    req = urllib.request.Request(
        f"{NOMINATIM_REVERSE_URL}?{params}", headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    code = data.get("address", {}).get("country_code")
    return code.lower() if code else None


def reverse_country_aws(lon, lat):
    import boto3

    resp = boto3.client("geo-places").reverse_geocode(
        QueryPosition=[lon, lat], MaxResults=1
    )
    items = resp.get("ResultItems", [])
    if not items:
        return None
    country = items[0].get("Address", {}).get("Country") or {}
    code = country.get("Code2") or country.get("Code3")
    return code.lower() if code else None


def reverse_country(lon, lat, provider="free"):
    """ISO country code (lowercase) for a point, or None if unknown.

    Used to confirm a manually-entered coordinate really sits in the US, since
    a lat/lon bounding box alone leaks across the Canadian/Mexican borders.
    """
    if provider == "aws":
        return reverse_country_aws(lon, lat)
    return reverse_country_free(lon, lat)
