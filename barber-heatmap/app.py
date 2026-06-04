"""
Barber Heatmap — Naples
Flask backend: searches Google Places API for barbers, computes a grid
of straight-line distances to the nearest barber, serves data to the frontend.
"""

import os
import math
import time
from functools import lru_cache

from dotenv import load_dotenv
import googlemaps
from flask import Flask, jsonify, send_from_directory
from flask import request as flask_request

load_dotenv()

app = Flask(__name__, static_folder="static")

# ---------------------------------------------------------------------------
# Google Maps client
# ---------------------------------------------------------------------------
API_KEY = os.getenv("GOOGLE_API_KEY")
if not API_KEY or API_KEY == "YOUR_API_KEY_HERE":
    print("WARNING: No valid GOOGLE_API_KEY found in .env")
    gmaps = None
else:
    gmaps = googlemaps.Client(key=API_KEY)

# ---------------------------------------------------------------------------
# Barber search keywords (Italian)
# ---------------------------------------------------------------------------
BARBER_KEYWORDS = [
    "barbiere",
    "barberia",
    "barbieri",
    "barbiere uomo",
    "parrucchiere uomo",
    "barber shop",
]

NAPLES_CENTER = (40.8359, 14.2488)
NAPLES_RADIUS = 15_000  # metres

# ---------------------------------------------------------------------------
# In-memory cache
# ---------------------------------------------------------------------------
_barbers_cache = None
_barbers_cache_ts = 0
CACHE_TTL = 86_400  # 24 hours


def search_barbers() -> list[dict]:
    """Search Places API for barbers in the Naples area using multiple
    Italian keywords.  Results are deduplicated by lat/lng."""
    if gmaps is None:
        return []

    seen: set[tuple[float, float]] = set()
    barbers: list[dict] = []

    for kw in BARBER_KEYWORDS:
        try:
            result = gmaps.places(
                query=kw,
                location=NAPLES_CENTER,
                radius=NAPLES_RADIUS,
                language="it",
            )
            for place in result.get("results", []):
                loc = place["geometry"]["location"]
                key = (round(loc["lat"], 5), round(loc["lng"], 5))
                if key not in seen:
                    seen.add(key)
                    barbers.append(
                        {
                            "name": place.get("name", ""),
                            "address": place.get("vicinity", place.get("formatted_address", "")),
                            "lat": loc["lat"],
                            "lng": loc["lng"],
                        }
                    )

            # Paginate if needed
            while "next_page_token" in result:
                time.sleep(2)
                result = gmaps.places(
                    query=kw,
                    location=NAPLES_CENTER,
                    radius=NAPLES_RADIUS,
                    language="it",
                    page_token=result["next_page_token"],
                )
                for place in result.get("results", []):
                    loc = place["geometry"]["location"]
                    key = (round(loc["lat"], 5), round(loc["lng"], 5))
                    if key not in seen:
                        seen.add(key)
                        barbers.append(
                            {
                                "name": place.get("name", ""),
                                "address": place.get("vicinity", place.get("formatted_address", "")),
                                "lat": loc["lat"],
                                "lng": loc["lng"],
                            }
                        )
        except Exception as exc:
            print(f"Places search for '{kw}' failed: {exc}")

    return barbers


# ---------------------------------------------------------------------------
# Haversine distance
# ---------------------------------------------------------------------------
def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two lat/lng points."""
    R = 6_371_000
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (math.sin(d_lat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(d_lon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def nearest_barber_distance(lat: float, lng: float,
                            barbers: list[dict]) -> float:
    """Return the straight-line distance (metres) to the closest barber."""
    best = float("inf")
    for b in barbers:
        d = haversine(lat, lng, b["lat"], b["lng"])
        if d < best:
            best = d
    return best


def build_heatmap_grid(barbers: list[dict],
                       lat_min: float = 40.77,
                       lat_max: float = 40.87,
                       lng_min: float = 14.15,
                       lng_max: float = 14.30,
                       step_m: float = 200) -> list[dict]:
    """Build a grid of points with distance-to-nearest-barber (metres).

    Step is approximate — we convert metres to degrees at Naples' latitude.
    """
    if not barbers:
        return []

    # Convert step to degrees
    lat_step = step_m / 111_320
    lng_step = step_m / (111_320 * math.cos(math.radians(40.8359)))

    points: list[dict] = []
    lat = lat_min
    while lat <= lat_max:
        lng = lng_min
        while lng <= lng_max:
            dist = nearest_barber_distance(lat, lng, barbers)
            points.append({"lat": round(lat, 5), "lng": round(lng, 5),
                           "distance_m": round(dist, 1)})
            lng += lng_step
        lat += lat_step

    return points


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/barbers")
def api_barbers():
    global _barbers_cache, _barbers_cache_ts
    now = time.time()
    if _barbers_cache is None or (now - _barbers_cache_ts) > CACHE_TTL:
        _barbers_cache = search_barbers()
        _barbers_cache_ts = now
    return jsonify({"count": len(_barbers_cache), "barbers": _barbers_cache})


@app.route("/api/heatmap")
def api_heatmap():
    global _barbers_cache, _barbers_cache_ts
    now = time.time()
    if _barbers_cache is None or (now - _barbers_cache_ts) > CACHE_TTL:
        _barbers_cache = search_barbers()
        _barbers_cache_ts = now

    if not _barbers_cache:
        return jsonify({"error": "No barbers found. Check your API key."}), 503

    # Optional query params to adjust grid bounds
    lat_min = flask_request.args.get("lat_min", 40.77, type=float)
    lat_max = flask_request.args.get("lat_max", 40.87, type=float)
    lng_min = flask_request.args.get("lng_min", 14.15, type=float)
    lng_max = flask_request.args.get("lng_max", 14.30, type=float)
    step = flask_request.args.get("step", 200, type=float)

    grid = build_heatmap_grid(_barbers_cache, lat_min, lat_max,
                              lng_min, lng_max, step)

    # Find max distance for normalisation on the frontend
    max_dist = max(p["distance_m"] for p in grid) if grid else 0

    return jsonify({
        "count": len(grid),
        "max_distance_m": max_dist,
        "points": grid,
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
