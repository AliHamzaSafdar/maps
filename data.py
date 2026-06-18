import os
import re
import sys
import argparse
from datetime import datetime, timezone
import requests
import pandas as pd
import time
from rapidfuzz import fuzz
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# ─── CONFIG ───────────────────────────────────────────────────────────────────
GOOGLE_API_KEY   = os.environ["GOOGLE_API_KEY"]
OSM_RADIUS_M     = 5000    # initial search radius (metres)
OSM_RADIUS_WIDE  = 15000   # wider retry radius for hard cases
SLEEP_BETWEEN    = 2       # seconds between rows

# ── Fuzzy score thresholds ────────────────────────────────────────────────────
FUZZY_AUTO      = 85   # score ≥ this → accept directly, NO Gemini call
FUZZY_CONSIDER  = 30   # score in [30, 84] → send to Gemini (ambiguous)
                       # score < 30 → skip Gemini, go straight to Nominatim
                       # special: brand ≤5 chars (abbreviation) → always Gemini

USER_AGENT = "GMAOS-FuelGeocoder/1.0 (gmaos-project; fuel-price-research)"
# ──────────────────────────────────────────────────────────────────────────────


# ─── Brand extraction (replaces hardcoded aliases) ────────────────────────────
# Strip these suffixes from station names before fuzzy comparison
_SUFFIXES = re.compile(
    r"""(
        \s*\#?\s*\d+        |  # store numbers:  #796 / 1243 / # 2
        \s+express\b        |  # "TA EXPRESS" → "TA"
        \s+travel\s+cent\w* |  # "TRAVEL CENTER(S)"
        \s+stopping\s+cent\w*| # "STOPPING CENTER"
        \s+truck\s+stop\b   |  # "TRUCK STOP"
        \s+truck\s+plaza\b  |  # "TRUCK PLAZA"
        \s+travel\s+stop\b  |  # "TRAVEL STOP"
        \s+fuel\s+stop\b    |  # "FUEL STOP"
        \s+general\s+store\b|  # "GENERAL STORE"
        \s+of\s+america\b   |  # "OF AMERICA"
        \s+flying\s+j\b        # "FLYING J"
    )""",
    re.VERBOSE | re.IGNORECASE,
)

def extract_brand(name: str) -> str:
    """
    Pull the core brand from a raw station name.
    e.g. "PILOT TRAVEL CENTER #1243" → "pilot"
         "TA SEYMOUR TRAVEL CENTER"  → "ta seymour"   (city still present)
         "KWIK TRIP #796"            → "kwik trip"
         "PETRO STOPPING CENTER #348"→ "petro"
    """
    brand = name.lower()
    brand = _SUFFIXES.sub("", brand)
    brand = re.sub(r"\s+", " ", brand).strip()
    return brand


def city_strip(name: str, city: str) -> str:
    """
    Remove city name tokens from station name before scoring.
    e.g. "WOODSHED OF BIG CABIN", city="Big Cabin"
         → "woodshed of"  → after extract_brand → "woodshed"
    This prevents city tokens from inflating or deflating fuzzy scores.
    """
    result = name.lower()
    for token in city.lower().split():
        if len(token) > 2:          # ignore tiny words like "of", "in"
            result = result.replace(token, "")
    return re.sub(r"\s+", " ", result).strip()


# ─── Fuzzy scorer ──────────────────────────────────────────────────────────────────────────────
def _score_pair(query_brand: str, query_full: str, osm_name: str) -> int:
    """
    Return the BEST across three comparisons:
      1. extracted brand vs extracted osm brand  (handles suffix stripping)
      2. full query vs full osm name             (catches exact-ish matches)
      3. WRatio on brands                        (handles partial/reordered words)
    Taking the max gives the highest chance of a confident match.
    """
    osm_brand = extract_brand(osm_name)
    s1 = fuzz.token_sort_ratio(query_brand, osm_brand)
    s2 = fuzz.token_sort_ratio(query_full.lower(), osm_name.lower())
    s3 = fuzz.WRatio(query_brand, osm_brand)        # handles partial containment
    return max(s1, s2, s3)


def fuzzy_pick_best(station_name: str, osm_stations: dict, city: str = ""):
    """
    Score every OSM candidate and return the best.

    Tier logic:
        'auto'      score ≥ FUZZY_AUTO              → accept, skip Gemini
        'consider'  FUZZY_CONSIDER ≤ score < AUTO   → send to Gemini
        'abbrev'    brand ≤ 5 chars (ACI, TA, etc.) → always Gemini (can't fuzzy-match abbreviations)
        'weak'      score < FUZZY_CONSIDER           → skip Gemini, go to Nominatim
    Returns None if osm_stations is empty.
    """
    if not osm_stations:
        return None

    # Strip city name tokens before scoring so e.g. "WOODSHED OF BIG CABIN"
    # becomes "woodshed of" before extract_brand turns it into "woodshed"
    name_no_city = city_strip(station_name, city) if city else station_name
    q_brand = extract_brand(name_no_city)
    q_full  = station_name

    # Abbreviation heuristic: single short token (ACI, TA, BP, etc.)
    # Fuzzy can never match these reliably → always send to Gemini
    if len(q_brand.replace(" ", "")) <= 5 and " " not in q_brand.strip():
        # Still find the best-scoring candidate to pass to Gemini
        scored = [
            (_score_pair(q_brand, q_full, n), n, c)
            for n, c in osm_stations.items()
        ]
        scored.sort(reverse=True)
        best_score, best_name, best_coords = scored[0]
        print(f"⚡ Short brand '{q_brand}' → forcing Gemini (abbreviation)")
        return best_name, best_coords, best_score, "abbrev"

    scored = [
        (_score_pair(q_brand, q_full, n), n, c)
        for n, c in osm_stations.items()
    ]
    scored.sort(reverse=True)

    best_score, best_name, best_coords = scored[0]

    if best_score >= FUZZY_AUTO:
        tier = "auto"
    elif best_score >= FUZZY_CONSIDER:
        tier = "consider"
    else:
        tier = "weak"

    return best_name, best_coords, best_score, tier


# ─── OSM Overpass (with city-level cache) ─────────────────────────────────────
_osm_cache: dict[tuple, dict] = {}   # (lat_rounded, lon_rounded, radius) → stations

def get_osm_stations(city_lat: float, city_lon: float, radius: int = OSM_RADIUS_M) -> dict:
    """
    Query Overpass for fuel/truck-stop nodes & ways near (city_lat, city_lon).
    Results are CACHED by (lat, lon, radius) so the same city is never queried twice.
    Returns dict: { station_name: (lat, lon) }
    """
    # Round to 3 decimals (~111 m precision) — same city hits same cache key
    cache_key = (round(city_lat, 3), round(city_lon, 3), radius)
    if cache_key in _osm_cache:
        return _osm_cache[cache_key]

    query = f"""[out:json][timeout:30];
(
  node["amenity"="fuel"](around:{radius},{city_lat},{city_lon});
  node["amenity"="truck_stop"](around:{radius},{city_lat},{city_lon});
  node["amenity"="convenience"](around:{radius},{city_lat},{city_lon});
  node["shop"="convenience"](around:{radius},{city_lat},{city_lon});
  node["shop"="gas"](around:{radius},{city_lat},{city_lon});
  node["highway"="services"](around:{radius},{city_lat},{city_lon});
  way["amenity"="fuel"](around:{radius},{city_lat},{city_lon});
  way["amenity"="truck_stop"](around:{radius},{city_lat},{city_lon});
  way["highway"="services"](around:{radius},{city_lat},{city_lon});
);
out center;"""

    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }

    def _post():
        return requests.post(
            "https://overpass-api.de/api/interpreter",
            data={"data": query},
            headers=headers,
            timeout=35,
        )

    try:
        r = _post()
        if r.status_code == 429:
            print("    ⚠️  Overpass rate-limited, waiting 30s...")
            time.sleep(5)
            r = _post()
        r.raise_for_status()

        stations = {}
        for el in r.json().get("elements", []):
            tags = el.get("tags", {})
            name = tags.get("name") or tags.get("name:en") or tags.get("brand")
            if not name:
                continue
            if el["type"] == "node":
                lat, lon = el["lat"], el["lon"]
            elif el["type"] == "way" and "center" in el:
                lat, lon = el["center"]["lat"], el["center"]["lon"]
            else:
                continue
            stations[name] = (lat, lon)

        _osm_cache[cache_key] = stations
        return stations

    except Exception as e:
        print(f"    ❌ Overpass error: {e}")
        return {}


# ─── Gemini AI picker (heavy-lifting only) ────────────────────────────────────
def ai_pick_best(station_name: str, osm_stations: dict):
    """
    ONLY called when fuzzy score is in the ambiguous 50–84 range.
    Passes only the top-10 fuzzy candidates (focused, cheaper prompt).
    Returns (name, (lat, lon)) or None.
    """
    names = list(osm_stations.keys())
    if not names:
        return None

    numbered = "\n".join(f"{i+1}. {n}" for i, n in enumerate(names))
    prompt = f"""Fuel station to match: "{station_name}"

Nearby OSM places:
{numbered}

Which number is the BEST match?
- Brand equivalences count ("PILOT TRAVEL CENTER" = "Pilot Flying J", "TA" prefix = "Travel Centers of America")
- Ignore store numbers (#123, #796)
- Only match if there is GENUINE brand/name similarity
- If NONE is a reasonable match, reply: 0

Reply with one number only."""

    try:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GOOGLE_API_KEY}",
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=30,
        )
        r.raise_for_status()
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        idx = int(text) - 1  # "0" → -1 means no match
        if idx < 0:
            return None
        chosen = names[idx]
        return chosen, osm_stations[chosen]

    except Exception as e:
        print(f"    ❌ Gemini error: {e}")
        return None


# ─── Nominatim fallback ───────────────────────────────────────────────────────
def nominatim_search(station_name: str, city: str, state: str):
    """
    Free OSM geocoder — no API key, no cost.
    Tries 3 query variants for better coverage.
    Returns (lat, lon, display_name) or None.
    """
    url     = "https://nominatim.openstreetmap.org/search"
    headers = {"User-Agent": USER_AGENT}
    clean   = re.sub(r"#\s*\d+", "", station_name).strip()
    brand   = extract_brand(station_name)          # shortest, most searchable form

    queries = [
        f"{clean}, {city}, {state}, USA",          # full clean name + city
        f"{brand}, {city}, {state}, USA",           # just brand + city
        f"{clean}, {state}, USA",                   # full name, state only
    ]

    for q in queries:
        try:
            time.sleep(1)  # Nominatim: max 1 req/sec
            r = requests.get(
                url,
                params={"q": q, "format": "json", "limit": 3,
                        "countrycodes": "us", "addressdetails": 1},
                headers=headers,
                timeout=15,
            )
            r.raise_for_status()
            results = r.json()
            if results:
                top = results[0]
                return float(top["lat"]), float(top["lon"]), top["display_name"]
        except Exception as e:
            print(f"    ❌ Nominatim error: {e}")

    return None


# ─── Full pipeline ────────────────────────────────────────────────────────────
def geocode_station(station_name: str, city: str, state: str,
                    city_lat: float, city_lon: float,
                    skip_ai: bool = False):
    """
    Decision tree:

      skip_ai=False (default):
        Fuzzy ≥ 85  → auto-accept
        Fuzzy 30-84 → Gemini
        Brand ≤5    → Gemini (abbreviation)
        Fuzzy < 30  → Nominatim

      skip_ai=True (--skip-ai flag):
        Fuzzy ≥ 85  → auto-accept
        anything else → Nominatim (Gemini never called)
    """

    def _try_radius(radius: int, label: str = ""):
        tag = f" [{label}]" if label else ""
        print(f"    Querying OSM (radius={radius}m){tag}...")
        osm = get_osm_stations(city_lat, city_lon, radius=radius)
        cached = "(cached)" if (round(city_lat,3), round(city_lon,3), radius) in _osm_cache else ""
        print(f"    Found {len(osm)} OSM candidates {cached}".strip())

        if not osm:
            return None

        fz = fuzzy_pick_best(station_name, osm, city=city)
        if fz is None:
            return None

        best_name, best_coords, score, tier = fz
        print(f"    🔍 Fuzzy: '{best_name}' score={score:.1f} → tier={tier}")

        # ── AUTO: high confidence, skip Gemini ─────────────────────────────────────
        if tier == "auto":
            lat, lon = best_coords
            print(f"    ✅ Fuzzy auto-match → ({lat:.6f}, {lon:.6f})")
            return lat, lon, "fuzzy_osm"

        # ── skip_ai=True: never call Gemini, fall through to Nominatim ────────────
        if skip_ai:
            print(f"    ⏭️  --skip-ai: score={score:.1f}, skipping Gemini")
            return None

        # ── CONSIDER / ABBREV: ambiguous or abbreviation → Gemini heavy-lifting ─
        if tier in ("consider", "abbrev"):
            name_no_city = city_strip(station_name, city) if city else station_name
            q_brand = extract_brand(name_no_city)
            top10 = dict(
                sorted(
                    osm.items(),
                    key=lambda kv: _score_pair(q_brand, station_name, kv[0]),
                    reverse=True,
                )[:10]
            )
            print(f"    🤖 Calling Gemini for {len(top10)} candidates...")
            ai = ai_pick_best(station_name, top10)
            if ai:
                name, (lat, lon) = ai
                print(f"    ✅ Gemini matched: '{name}' → ({lat:.6f}, {lon:.6f})")
                return lat, lon, "ai_osm"
            print("    ⚠️  Gemini found no match")
            return None

        # ── WEAK: score too low, don't waste Gemini ──────────────────────────
        print(f"    ⚠️  Score too low ({score}) — skipping Gemini")
        return None

    # Step 1: normal radius
    result = _try_radius(OSM_RADIUS_M)
    if result:
        return result

    # Step 2: Nominatim (free, no Gemini)
    print("    Trying Nominatim...")
    nom = nominatim_search(station_name, city, state)
    if nom:
        lat, lon, display = nom
        print(f"    ✅ Nominatim → ({lat:.6f}, {lon:.6f})  '{display[:55]}...'")
        return lat, lon, "nominatim"

    # Step 3: wider radius retry
    print(f"    🔄 Wider radius retry ({OSM_RADIUS_WIDE}m)...")
    result = _try_radius(OSM_RADIUS_WIDE, label="wide")
    if result:
        lat, lon, status = result
        return lat, lon, status + "_wide"

    # Step 4: give up
    print("    ❌ Not found anywhere")
    return 0.0, 0.0, "not_found"


# ─── ARGS ─────────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="Geocode fuel stations via OSM + Nominatim (+ optional Gemini)")
parser.add_argument(
    "--use-ai",
    action="store_true",
    default=False,
    help="Enable Gemini AI for ambiguous matches. Default: OFF. Use on leftovers after the first pass.",
)
parser.add_argument(
    "--rows",
    type=int,
    default=10,
    help="Number of rows to process from the top of the CSV (default: 500).",
)
parser.add_argument(
    "--input",
    type=str,
    default="fuel-prices-with-coords.csv",
    help="Input CSV (default: fuel-prices-with-coords.csv).",
)
args = parser.parse_args()

USE_AI     = args.use_ai          # False by default — AI is OFF unless --use-ai is passed
SKIP_AI    = not USE_AI           # True by default  → Gemini never called unless --use-ai
mode_label = "with_ai" if USE_AI else "no_ai"

print("=" * 70)
print(f"Mode     : {'fuzzy + nominatim + Gemini AI  (--use-ai)' if USE_AI else 'fuzzy + nominatim ONLY  (default)'}")
print(f"Input    : {args.input}")
print(f"Rows     : {args.rows}")
print("=" * 70)

# ─── MAIN ─────────────────────────────────────────────────────────────────────
cdf_temp = pd.read_csv(args.input)
subset   = cdf_temp.head(args.rows).copy()

exact_lats, exact_lons, match_statuses, geocoded_ats = [], [], [], []
gemini_calls = 0

_orig_ai = ai_pick_best
def ai_pick_best_tracked(name, osm):
    global gemini_calls
    gemini_calls += 1
    return _orig_ai(name, osm)
ai_pick_best = ai_pick_best_tracked

for i, row in subset.iterrows():
    print(f"\n[{i+1}/{len(subset)}] {row['Truckstop_Name']}, {row['City']}, {row['State']}")

    # Guard: rows with no city coords → skip OSM, go straight to Nominatim
    c_lat = row.get("city_lat")
    c_lon = row.get("city_lon")
    has_coords = pd.notna(c_lat) and pd.notna(c_lon)
    if not has_coords:
        print("    ⚠️  No city coords — skipping OSM, trying Nominatim only")
        nom = nominatim_search(row["Truckstop_Name"], row["City"], row["State"])
        if nom:
            lat, lon, display = nom
            print(f"    ✅ Nominatim → ({lat:.6f}, {lon:.6f})  '{display[:55]}...'")
            status = "nominatim"
        else:
            lat, lon, status = 0.0, 0.0, "not_found"
    else:
        lat, lon, status = geocode_station(
            station_name=row["Truckstop_Name"],
            city=row["City"],
            state=row["State"],
            city_lat=float(c_lat),
            city_lon=float(c_lon),
            skip_ai=SKIP_AI,
        )

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    exact_lats.append(lat)
    exact_lons.append(lon)
    match_statuses.append(status)
    geocoded_ats.append(ts)
    time.sleep(SLEEP_BETWEEN)

subset["exact_lat"]    = exact_lats
subset["exact_lon"]    = exact_lons
subset["match_status"] = match_statuses
subset["geocoded_at"]  = geocoded_ats

# ─── SAVE ────────────────────────────────────────────────────────────────────────
run_ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
out_file = f"geocoded_{mode_label}_{run_ts}.csv"
subset.to_csv(out_file, index=False)
print(f"\n💾 Saved → {out_file}")

# ─── SUMMARY ────────────────────────────────────────────────────────────────────
print("\n" + "=" * 70)
if not SKIP_AI:
    print(f"Gemini API calls used: {gemini_calls} / {len(subset)} rows")
print("Summary:")
print(subset["match_status"].value_counts().to_string())
print("=" * 70)
print(subset[["Truckstop_Name", "City", "State", "exact_lat", "exact_lon", "match_status", "geocoded_at"]].to_string())