"""Fetch raw OpenStreetMap data for a bounding box via the Overpass API.

Cached aggressively on disk: during a demo we must never depend on Overpass
being up. A cached extract is a first-class asset, not an optimisation.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import requests

from ..config import CACHE_DIR
from ..contracts import BBox

# Mirrors, tried in order. The main endpoint rate-limits aggressively.
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

# Road types we care about for vehicular simulation. Deliberately excludes
# footways/paths -- they add nodes and cost build time without affecting traffic.
HIGHWAY_FILTER = (
    "motorway|trunk|primary|secondary|tertiary|unclassified|residential|"
    "living_street|service|motorway_link|trunk_link|primary_link|"
    "secondary_link|tertiary_link|road"
)

QUERY_TEMPLATE = """
[out:xml][timeout:{timeout}];
(
  way["highway"~"^({filter})$"]({bbox});
  node["highway"="traffic_signals"]({bbox});
  node["highway"="crossing"]({bbox});
);
(._;>;);
out body;
"""


def cache_key(bbox: BBox) -> str:
    raw = f"{bbox.south:.6f},{bbox.west:.6f},{bbox.north:.6f},{bbox.east:.6f}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def fetch_osm(bbox: BBox, timeout: int = 180, force: bool = False) -> Path:
    """Download the OSM extract for `bbox`, returning the path to the .osm file.

    Raises RuntimeError only if every mirror fails AND no cache exists.
    """
    out_path = CACHE_DIR / f"osm_{cache_key(bbox)}.osm.xml"
    if out_path.exists() and out_path.stat().st_size > 1024 and not force:
        return out_path

    query = QUERY_TEMPLATE.format(
        timeout=timeout, filter=HIGHWAY_FILTER, bbox=bbox.as_overpass()
    )

    last_error: Exception | None = None
    for endpoint in OVERPASS_ENDPOINTS:
        try:
            response = requests.post(
                endpoint,
                data={"data": query},
                timeout=timeout,
                headers={"User-Agent": "RoadTwin/1.0 (SIH25100 traffic simulation)"},
            )
            if response.status_code == 200 and len(response.content) > 1024:
                out_path.write_bytes(response.content)
                return out_path
            last_error = RuntimeError(
                f"{endpoint} returned HTTP {response.status_code} "
                f"({len(response.content)} bytes)"
            )
        except Exception as exc:  # noqa: BLE001 - any transport failure, try next
            last_error = exc
        time.sleep(1.0)

    if out_path.exists():
        return out_path  # stale cache beats no data during a live demo
    raise RuntimeError(f"All Overpass mirrors failed. Last error: {last_error}")
