"""
Barber Heatmap — Naples
Flask backend: searches Google Places API for barbers, computes a grid
of straight-line distances to the nearest barber, serves data to the frontend.
All API results cached to disk — zero ongoing API costs after first run.
Sea points excluded via Overpass-admin boundary (cached forever).
"""

import json
import math
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
import googlemaps
from flask import Flask, jsonify, send_from_directory
from flask import request as flask_request

load_dotenv()

app = Flask(__name__, static_folder="static")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
CACHE_DIR = BASE_DIR / "cache"
CACHE_DIR.mkdir(exist_ok=True)

BARBER_CACHE = CACHE_DIR / "barbers.json"
GRID_CACHE = CACHE_DIR / "heatmap_grid.json"
BOUNDARY_CACHE = CACHE_DIR / "naples_boundary.json"

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

# ── Grid defaults ──────────────────────────────────────────────────────────
GRID_LAT_MIN = 40.77
GRID_LAT_MAX = 40.87
GRID_LNG_MIN = 14.15
GRID_LNG_MAX = 14.30
GRID_STEP_M = 100  # 100 m grid for finer detail


# ===================================================================
#  DISK CACHE helpers
# ===================================================================
def _read_cache(path: Path, max_age: float | None = None):
    """Return cached dict/list or None if missing / expired."""
    if not path.exists():
        return None
    if max_age is not None and time.time() - path.stat().st_mtime > max_age:
        return None
    with open(path) as f:
        return json.load(f)


def _write_cache(path: Path, data):
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ===================================================================
#  NAPLES LAND BOUNDARY  (Overpass API, cached forever on disk)
# ===================================================================
# ── Boundary cache (in-memory to avoid repeated Overpass calls) ─────
_boundary_cache: list[list[tuple[float, float]]] | None | bool = None
#  None = not fetched yet, list = success, False = failed / unavailable


def _fetch_naples_boundary() -> list[list[tuple[float, float]]] | None:
    """Fetch Napoli comune boundary from Overpass API.

    Returns a list of polygon rings (each ring is a list of (lat, lon)
    tuples).  Cached in memory AND on disk so the call is made at most once.
    """
    global _boundary_cache

    # ── in-memory check ────────────────────────────────────────────
    if _boundary_cache is not None:
        return _boundary_cache if _boundary_cache else None
    if _boundary_cache is False:
        return None

    # ── disk check ─────────────────────────────────────────────────
    cached = _read_cache(BOUNDARY_CACHE)
    if cached is not None:
        rings = [[tuple(t) for t in ring] for ring in cached]
        _boundary_cache = rings
        return rings

    # ── Overpass fetch ─────────────────────────────────────────────
    overpass_url = "https://overpass-api.de/api/interpreter"
    query = ("[out:json];"
             "relation[\"name\"=\"Napoli\"]"
             "[\"admin_level\"=\"8\"]"
             "[\"boundary\"=\"administrative\"];"
             "out geom;")
    try:
        resp = requests.post(
            overpass_url,
            data={"data": query},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=45,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        print(f"Overpass query failed ({exc}) — using rough polygon fallback")
        _boundary_cache = False
        return None

    rings: list[list[tuple[float, float]]] = []
    for elem in data.get("elements", []):
        if elem.get("type") != "relation":
            continue
        for member in elem.get("members", []):
            geom = member.get("geometry")
            if not geom or member.get("role") == "inner":
                continue
            ring = [(g["lat"], g["lon"]) for g in geom]
            if ring:
                rings.append(ring)

    if rings:
        _write_cache(BOUNDARY_CACHE, rings)
        _boundary_cache = rings
        return rings

    _boundary_cache = False
    return None


# ── rough fallback polygon if Overpass is unreachable ──────────────
_ROUGH_NAPLES_POLYGON: list[tuple[float, float]] = [
    # ── coastline (south edge, west → east) ──
    # Starting west of Bagnoli, going east along the shore
    (40.805, 14.148), (40.806, 14.155), (40.807, 14.162),
    (40.808, 14.168), (40.809, 14.174),  # Bagnoli / Coroglio
    # Posillipo peninsula — extends south into the bay
    (40.809, 14.178), (40.810, 14.183), (40.811, 14.188),
    (40.813, 14.193), (40.815, 14.198), (40.818, 14.203),
    # Mergellina / Piedigrotta
    (40.822, 14.210), (40.825, 14.216), (40.827, 14.222),
    # Chiaia / Santa Lucia
    (40.830, 14.228), (40.833, 14.234), (40.835, 14.240),
    # San Ferdinando / Porto
    (40.837, 14.245), (40.839, 14.250), (40.841, 14.255),
    # Mercato / Porto Orientale
    (40.844, 14.261), (40.847, 14.267), (40.850, 14.273),
    # San Giovanni a Teduccio / east coast
    (40.853, 14.279), (40.856, 14.285), (40.859, 14.290),
    (40.862, 14.294),
    # ── east / north-east suburbs ──
    (40.866, 14.298), (40.870, 14.301), (40.874, 14.303),
    (40.878, 14.304), (40.882, 14.302), (40.885, 14.298),
    # ── north (inland) — generous buffer ──
    (40.887, 14.292), (40.888, 14.284), (40.888, 14.274),
    (40.887, 14.262), (40.886, 14.250), (40.884, 14.238),
    (40.882, 14.226), (40.879, 14.215), (40.876, 14.204),
    (40.872, 14.194), (40.868, 14.185), (40.863, 14.177),
    (40.858, 14.170), (40.852, 14.165), (40.846, 14.160),
    # ── west / north-west suburbs ──
    (40.840, 14.156), (40.833, 14.153), (40.826, 14.151),
    (40.818, 14.150), (40.811, 14.149),
]


# ── point-in-polygon (ray casting) ─────────────────────────────────
def _point_in_ring(lat: float, lon: float,
                   ring: list[tuple[float, float]]) -> bool:
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        yi, xi = ring[i]
        yj, xj = ring[j]
        if ((xi > lon) != (xj > lon)) and \
           lat < (yj - yi) * (lon - xi) / (xj - xi) + yi:
            inside = not inside
        j = i
    return inside


def _is_on_land(lat: float, lon: float) -> bool:
    """Return True if the point is within the Naples land boundary."""
    rings = _fetch_naples_boundary()
    if rings is None:
        rings = [_ROUGH_NAPLES_POLYGON]
    for ring in rings:
        if _point_in_ring(lat, lon, ring):
            return True
    return False


# ===================================================================
#  SEARCH BARBERS  (Places API — cached to disk 7 days)
# ===================================================================
def search_barbers() -> list[dict]:
    """Search Places API for barbers — caches result to disk for 7 days."""
    cached = _read_cache(BARBER_CACHE, max_age=7 * 86_400)
    if cached is not None:
        print("Using cached barbers (disk)")
        return cached

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
                    barbers.append({
                        "name": place.get("name", ""),
                        "address": place.get("vicinity",
                                             place.get("formatted_address", "")),
                        "lat": loc["lat"],
                        "lng": loc["lng"],
                    })
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
                        barbers.append({
                            "name": place.get("name", ""),
                            "address": place.get("vicinity",
                                                 place.get("formatted_address", "")),
                            "lat": loc["lat"],
                            "lng": loc["lng"],
                        })
        except Exception as exc:
            print(f"Places search for '{kw}' failed: {exc}")

    print(f"Fetched {len(barbers)} barbers from Places API")
    _write_cache(BARBER_CACHE, barbers)
    return barbers


# ===================================================================
#  HAVERSINE  & nearest-barber distance
# ===================================================================
def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (math.sin(d_lat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(d_lon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def nearest_barber_distance(lat: float, lng: float,
                            barbers: list[dict]) -> float:
    best = float("inf")
    for b in barbers:
        d = haversine(lat, lng, b["lat"], b["lng"])
        if d < best:
            best = d
    return best


# ===================================================================
#  BUILD GRID  (only on-land points, cached to disk)
# ===================================================================
def _grid_cache_key(lat_min, lat_max, lng_min, lng_max, step_m) -> Path:
    stem = f"grid_{step_m}m_{lat_min}_{lat_max}_{lng_min}_{lng_max}"
    return CACHE_DIR / f"{stem}.json"


def build_heatmap_grid(barbers: list[dict],
                       lat_min: float = GRID_LAT_MIN,
                       lat_max: float = GRID_LAT_MAX,
                       lng_min: float = GRID_LNG_MIN,
                       lng_max: float = GRID_LNG_MAX,
                       step_m: float = GRID_STEP_M) -> dict:
    """Build grid → only land points → cache to disk.

    Returns dict with points, bounds, cols, rows, step info.
    """
    cache_path = _grid_cache_key(lat_min, lat_max, lng_min, lng_max, step_m)
    cached = _read_cache(cache_path, max_age=7 * 86_400)
    if cached is not None:
        print("Using cached heatmap grid (disk)")
        return cached

    if not barbers:
        return {"points": [], "cols": 0, "rows": 0,
                "max_distance_m": 0, "bounds": {},
                "lat_step": 0, "lng_step": 0, "step_m": step_m}

    lat_step = step_m / 111_320
    lng_step = step_m / (111_320 * math.cos(math.radians(40.8359)))

    points: list[dict] = []
    cols = 0
    rows = 0

    # iterate north → south  (so row 0 = top of canvas)
    lat = lat_max
    while lat >= lat_min:
        row_cols = 0
        lng = lng_min
        while lng <= lng_max:
            if _is_on_land(lat, lng):
                dist = nearest_barber_distance(lat, lng, barbers)
                points.append({"lat": round(lat, 5), "lng": round(lng, 5),
                               "distance_m": round(dist, 1)})
            else:
                # sea point → mark as null so frontend can render transparent
                points.append({"lat": round(lat, 5), "lng": round(lng, 5),
                               "distance_m": None})
            row_cols += 1
            lng += lng_step
        cols = max(cols, row_cols)
        rows += 1
        lat -= lat_step

    max_dist = max((p["distance_m"] for p in points if p["distance_m"] is not None),
                   default=0)

    result = {
        "points": points,
        "cols": cols,
        "rows": rows,
        "max_distance_m": max_dist,
        "bounds": {"lat_min": lat_min, "lat_max": lat_max,
                   "lng_min": lng_min, "lng_max": lng_max},
        "lat_step": lat_step,
        "lng_step": lng_step,
        "step_m": step_m,
    }
    _write_cache(cache_path, result)
    return result


# ===================================================================
#  ROUTES
# ===================================================================
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/barbers")
def api_barbers():
    barbers = search_barbers()
    return jsonify({"count": len(barbers), "barbers": barbers})


@app.route("/api/heatmap")
def api_heatmap():
    barbers = search_barbers()
    if not barbers:
        return jsonify({"error": "No barbers found. Check your API key."}), 503

    lat_min = flask_request.args.get("lat_min", GRID_LAT_MIN, type=float)
    lat_max = flask_request.args.get("lat_max", GRID_LAT_MAX, type=float)
    lng_min = flask_request.args.get("lng_min", GRID_LNG_MIN, type=float)
    lng_max = flask_request.args.get("lng_max", GRID_LNG_MAX, type=float)
    step = flask_request.args.get("step", GRID_STEP_M, type=float)

    result = build_heatmap_grid(barbers, lat_min, lat_max,
                                lng_min, lng_max, step)
    return jsonify(result)


# ===================================================================
#  ENTRY POINT
# ===================================================================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
