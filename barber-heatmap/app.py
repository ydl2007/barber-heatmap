"""
Barber Heatmap — Naples
Flask backend: searches Google Places API for barbers, computes a grid
of straight-line distances to the nearest barber, serves data to the frontend.
All API results cached to disk — zero ongoing API costs after first run.
Sea points excluded via Overpass-admin boundary (cached forever).
"""

import base64
import io
import json
import math
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
import googlemaps
from PIL import Image
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
GRID_LAT_MAX = 40.92
GRID_LNG_MIN = 14.12
GRID_LNG_MAX = 14.36
GRID_STEP_M = 20   # 20 m grid — fine detail, computed once, cached to disk


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
    # ═══════════════════════════════════════════════════════════════
    #  COASTLINE — generous southern boundary
    #  Purposely dips WELL south of the actual coast to ensure no
    #  coastal neighbourhoods (Posillipo, S.Ferdinando, etc.) are cut off.
    # ═══════════════════════════════════════════════════════════════
    # Bagnoli / Coroglio (western edge)
    (40.800, 14.145), (40.801, 14.152), (40.802, 14.160),
    (40.803, 14.167), (40.804, 14.173),
    # Posillipo peninsula — dips far south
    (40.804, 14.178), (40.803, 14.183), (40.803, 14.187),
    (40.804, 14.191), (40.806, 14.195), (40.808, 14.198),
    # Posillipo curves back north toward Mergellina
    (40.811, 14.202), (40.815, 14.206),
    # Mergellina / Piedigrotta
    (40.819, 14.211), (40.823, 14.217), (40.826, 14.223),
    # Chiaia / Santa Lucia (dip south again for the port area)
    (40.829, 14.229), (40.832, 14.235), (40.834, 14.240),
    # San Ferdinando / Porto
    (40.836, 14.245), (40.838, 14.250), (40.840, 14.255),
    # Mercato / Porto Orientale
    (40.842, 14.260), (40.845, 14.266), (40.848, 14.272),
    # San Giovanni a Teduccio / eastern coast
    (40.851, 14.278), (40.854, 14.284), (40.857, 14.289),
    (40.860, 14.293),
    # ── east / north-east suburbs ──
    (40.864, 14.297), (40.868, 14.300), (40.873, 14.303),
    (40.877, 14.304), (40.881, 14.303), (40.885, 14.300),
    # ── north (inland) — generous buffer ──
    (40.888, 14.295), (40.889, 14.286), (40.889, 14.276),
    (40.888, 14.264), (40.887, 14.252), (40.885, 14.240),
    (40.883, 14.228), (40.880, 14.217), (40.877, 14.206),
    (40.873, 14.196), (40.869, 14.187), (40.864, 14.179),
    (40.859, 14.172), (40.853, 14.166), (40.847, 14.162),
    # ── west / north-west suburbs ──
    (40.841, 14.158), (40.834, 14.155), (40.827, 14.152),
    (40.819, 14.150), (40.812, 14.148), (40.806, 14.146),
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
        "count": len(points),
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
#  RENDER GRID IMAGE  (server-side canvas via Pillow)
# ===================================================================
def _dist_to_color(distance_m: float | None, max_dist: float):
    """Match the frontend distToCSS logic — returns RGBA tuple."""
    if distance_m is None:
        return (0, 0, 0, 0)  # transparent for sea
    t = min(distance_m / max_dist, 1) if max_dist > 0 else 0
    curved = t ** 0.6
    # hue: 0° = red (close), 240° = blue (far)
    hue = 240 * curved  # 0-240 degrees
    # Convert HSL to RGB
    h = hue / 360
    r, g, b = _hsl_to_rgb(h, 0.5, 0.5)
    return (int(r * 255), int(g * 255), int(b * 255), 255)


def _hsl_to_rgb(h, s, l):
    """HSL → RGB (all values 0-1)."""
    if s == 0:
        return (l, l, l)
    def _hue_to_rgb(p, q, t):
        if t < 0: t += 1
        if t > 1: t -= 1
        if t < 1/6: return p + (q - p) * 6 * t
        if t < 1/2: return q
        if t < 2/3: return p + (q - p) * (2/3 - t) * 6
        return p
    q = l * (1 + s) if l < 0.5 else l + s - l * s
    p = 2 * l - q
    return (_hue_to_rgb(p, q, h + 1/3),
            _hue_to_rgb(p, q, h),
            _hue_to_rgb(p, q, h - 1/3))


def render_grid_image(grid_result: dict) -> str:
    """Render the grid to a PNG and return a data URL."""
    points = grid_result["points"]
    cols = grid_result["cols"]
    rows = grid_result["rows"]
    max_dist = grid_result["max_distance_m"]

    if not points or cols == 0 or rows == 0:
        return ""

    CELL = 2  # pixels per cell
    img = Image.new("RGBA", (cols * CELL, rows * CELL), (0, 0, 0, 0))
    pixels = img.load()

    for i, p in enumerate(points):
        col = i % cols
        row = i // cols
        color = _dist_to_color(p["distance_m"], max_dist)
        # Fill the CELL×CELL block
        for dx in range(CELL):
            for dy in range(CELL):
                px = col * CELL + dx
                py = row * CELL + dy
                if px < img.width and py < img.height:
                    pixels[px, py] = color

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/png;base64,{b64}"


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

    # Render the grid to a PNG image (avoids sending 350k+ points over the wire)
    image_url = render_grid_image(result)

    return jsonify({
        "count": result["count"],
        "max_distance_m": result["max_distance_m"],
        "cols": result["cols"],
        "rows": result["rows"],
        "bounds": result["bounds"],
        "step_m": result["step_m"],
        "image": image_url,
    })


# ===================================================================
#  ENTRY POINT
# ===================================================================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
