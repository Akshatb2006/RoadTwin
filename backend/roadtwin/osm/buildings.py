"""Building footprints and heights -- the physical layer of the twin.

Reconnaissance result that shaped this module: for Koramangala, OSM has 5,971
building footprints but only 1.0% carry any height information (57 with
`building:levels`, zero with `height`). Footprints are excellent; heights are
absent. So the two halves need different sources, and the height source has to
be swappable rather than hard-wired.

HEIGHT SOURCES, in descending order of trust:
  1. OSM `height` tag                -- surveyed, metres. Trust it.
  2. OSM `building:levels`           -- count x storey height. Very reliable.
  3. Morphological estimate          -- inferred from footprint area, building
                                        type and the class of the road it sits
                                        on. A prior, not a measurement.

Every building carries `height_source` so the UI can show what is measured and
what is inferred. A 3D city that silently presents guesses as survey data would
be exactly the kind of thing this project refuses to do elsewhere.

Slot 0 is reserved for a raster height source (Google Open Buildings 2.5D,
CC-BY-4.0/ODbL, 4 m effective resolution, MAE 1.5 m). Its GCS layout is
resolved and documented in `HEIGHT_RASTER_NOTE` below; wiring it in only
requires a windowed read, because the rest of this pipeline does not care where
a height came from.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import requests

from ..config import CACHE_DIR
from ..contracts import BBox

HEIGHT_RASTER_NOTE = """
Google Open Buildings 2.5D Temporal, verified reachable anonymously:
  bucket    gs://open-buildings-temporal-data/v1
  manifests v1/manifests/{shard}_EPSG_{utm}_{year}_06_30.json
  tiles     v1/geotiffs/{cell}_{year}_06_30/tile_{id}.tif
  bands     0=building_fractional_count 1=building_height 2=building_presence
  pixel     0.5 m (≈4 m effective), FLOAT, nodata -99.0
  licence   CC-BY-4.0 / ODbL-1.0
Tiles are 25000x25000 px (~2 GB uncompressed), so any integration must do a
windowed /vsicurl read over the study bbox, never a whole-tile download.
Caveat found during recon: the EPSG:32643 manifest covers northing 2.4-4.1 Mm,
which does not include Bengaluru (~1.43 Mm) -- the correct shard for southern
India still needs to be identified before this source can be enabled.
"""

# Typical storey height in Indian urban construction (floor-to-floor).
STOREY_M = 3.2

# Morphological priors: (typical levels, minimum plausible levels).
TYPE_LEVELS: dict[str, float] = {
    "apartments": 4.0,
    "residential": 3.0,
    "house": 2.0,
    "detached": 2.0,
    "commercial": 4.0,
    "retail": 2.0,
    "office": 5.0,
    "industrial": 2.0,
    "warehouse": 1.0,
    "school": 3.0,
    "college": 4.0,
    "university": 4.0,
    "hospital": 5.0,
    "hotel": 5.0,
    "temple": 2.0,
    "mosque": 2.0,
    "church": 2.0,
    "garage": 1.0,
    "shed": 1.0,
    "hut": 1.0,
    "roof": 1.0,
    "construction": 3.0,
    "yes": 3.0,
}

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

QUERY = """
[out:json][timeout:{timeout}];
(
  way["building"]({bbox});
  relation["building"]["type"="multipolygon"]({bbox});
);
out geom tags;
"""


def _cache_key(bbox: BBox) -> str:
    raw = f"b|{bbox.south:.6f},{bbox.west:.6f},{bbox.north:.6f},{bbox.east:.6f}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def _ring_area_m2(ring: list[list[float]], lat0: float) -> float:
    """Shoelace area of a lon/lat ring, converted to square metres locally."""
    if len(ring) < 3:
        return 0.0
    mx = 111_320.0 * math.cos(math.radians(lat0))
    my = 110_540.0
    total = 0.0
    for i in range(len(ring) - 1):
        x1, y1 = ring[i][0] * mx, ring[i][1] * my
        x2, y2 = ring[i + 1][0] * mx, ring[i + 1][1] * my
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def _parse_height(tags: dict) -> tuple[float, str] | None:
    """Use a surveyed height or storey count when OSM actually has one."""
    raw = tags.get("height")
    if raw:
        try:
            value = float(str(raw).replace("m", "").strip())
            if 1.5 <= value <= 400:
                return value, "osm_height"
        except ValueError:
            pass
    levels = tags.get("building:levels")
    if levels:
        try:
            count = float(str(levels).split(";")[0].strip())
            if 0.5 <= count <= 120:
                return count * STOREY_M, "osm_levels"
        except ValueError:
            pass
    return None


def _estimate_height(tags: dict, area_m2: float) -> tuple[float, str]:
    """Morphological prior. Explicitly an estimate, and labelled as one.

    Larger footprints in Indian cities correlate with taller construction
    (apartment blocks and commercial towers), while small plots are typically
    two to three storeys. The area term is deliberately weak -- it nudges the
    type prior rather than driving it.
    """
    building_type = str(tags.get("building", "yes")).lower()
    levels = TYPE_LEVELS.get(building_type, 3.0)

    if area_m2 > 4000:
        levels += 3.0
    elif area_m2 > 1500:
        levels += 1.5
    elif area_m2 < 60:
        levels = min(levels, 2.0)

    if tags.get("building:part") == "yes":
        levels = max(1.0, levels - 1.0)

    return round(max(1.0, levels) * STOREY_M, 1), "estimated"


def fetch_buildings(bbox: BBox, force: bool = False) -> dict:
    """Return a GeoJSON FeatureCollection of buildings with heights.

    Cached like the road network: a demo must never depend on Overpass being up.
    """
    out_path = CACHE_DIR / f"buildings_{_cache_key(bbox)}.geojson"
    if out_path.exists() and out_path.stat().st_size > 512 and not force:
        return json.loads(out_path.read_text(encoding="utf-8"))

    query = QUERY.format(timeout=180, bbox=bbox.as_overpass())
    elements = None
    last_error: Exception | None = None
    for endpoint in OVERPASS_ENDPOINTS:
        try:
            response = requests.post(
                endpoint,
                data={"data": query},
                timeout=240,
                headers={"User-Agent": "RoadTwin/1.0 (SIH25100 digital twin)"},
            )
            if response.status_code == 200:
                elements = response.json().get("elements", [])
                break
            last_error = RuntimeError(f"{endpoint} -> HTTP {response.status_code}")
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    if elements is None:
        if out_path.exists():
            return json.loads(out_path.read_text(encoding="utf-8"))
        raise RuntimeError(f"Could not fetch buildings: {last_error}")

    centre_lat = bbox.center.lat
    features = []
    counts = {"osm_height": 0, "osm_levels": 0, "estimated": 0}

    for element in elements:
        geometry = element.get("geometry")
        if not geometry or len(geometry) < 4:
            continue
        ring = [[round(p["lon"], 6), round(p["lat"], 6)] for p in geometry]
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        if len(ring) < 4:
            continue

        tags = element.get("tags", {})
        area = _ring_area_m2(ring, centre_lat)
        if area < 12:  # sheds and mapping noise; they only add draw calls
            continue

        measured = _parse_height(tags)
        if measured:
            height, source = measured
        else:
            height, source = _estimate_height(tags, area)
        counts[source] = counts.get(source, 0) + 1

        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [ring]},
                "properties": {
                    "id": str(element.get("id")),
                    "height": round(height, 1),
                    "height_source": source,
                    "measured": source != "estimated",
                    "building": str(tags.get("building", "yes")),
                    "name": tags.get("name", ""),
                    "area_m2": round(area, 1),
                },
            }
        )

    collection = {
        "type": "FeatureCollection",
        "features": features,
        "properties": {
            "count": len(features),
            "height_sources": counts,
            "measured_pct": round(
                100.0
                * (counts["osm_height"] + counts["osm_levels"])
                / max(len(features), 1),
                1,
            ),
            "storey_m": STOREY_M,
        },
    }
    out_path.write_text(json.dumps(collection), encoding="utf-8")
    return collection
