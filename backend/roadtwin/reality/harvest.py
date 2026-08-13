"""Download a Mapillary sequence as an ordered image set for reconstruction.

Structure-from-Motion does not care about GPS, but we keep it anyway: the
camera positions give us a georeferenced scale and orientation for the finished
scene, which is what lets SUMO vehicles be placed in it later. A reconstruction
in arbitrary units cannot host a simulation.

Frames are written in capture order with zero-padded names because COLMAP's
sequential matcher relies on filename order to know which images are
neighbours. Feeding it shuffled filenames turns an easy sequential problem into
an exhaustive one.
"""

from __future__ import annotations

import concurrent.futures
import json
import math
from pathlib import Path

import requests

from ..config import DATA_DIR

GRAPH = "https://graph.mapillary.com"
REALITY_DIR = DATA_DIR / "reality"

# 2048 px is the largest thumbnail available without elevated permissions and
# is comfortably enough detail for a street-scale reconstruction.
IMAGE_FIELD = "thumb_2048_url"


def _haversine_m(a, b) -> float:
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * 6_371_000 * math.asin(min(1.0, math.sqrt(h)))


def sequence_image_ids(sequence_id: str, token: str) -> list[str]:
    response = requests.get(
        f"{GRAPH}/image_ids",
        params={"sequence_id": sequence_id, "access_token": token},
        timeout=120,
    )
    response.raise_for_status()
    return [item["id"] for item in response.json().get("data", [])]


def image_metadata(image_ids: list[str], token: str) -> list[dict]:
    """Fetch per-image metadata. The Graph API has no batch endpoint, so this
    is parallelised -- serially it is several minutes for a few hundred frames."""
    fields = (
        f"id,captured_at,compass_angle,computed_geometry,geometry,"
        f"camera_type,width,height,{IMAGE_FIELD}"
    )

    def one(image_id: str) -> dict | None:
        try:
            r = requests.get(
                f"{GRAPH}/{image_id}",
                params={"fields": fields, "access_token": token},
                timeout=60,
            )
            return r.json() if r.status_code == 200 else None
        except Exception:  # noqa: BLE001
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(one, image_ids))
    return [r for r in results if r and r.get(IMAGE_FIELD)]


def harvest_sequence(
    sequence_id: str,
    token: str,
    label: str,
    max_gap_m: float = 25.0,
) -> dict:
    """Download one sequence, keeping only its longest continuous run."""
    out_dir = REALITY_DIR / label
    images_dir = out_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    meta = image_metadata(sequence_image_ids(sequence_id, token), token)
    meta.sort(key=lambda m: m.get("captured_at") or 0)

    # Keep the longest unbroken run: a reconstruction spanning a 500 m jump
    # will either fail or silently split into two disconnected models.
    points = [
        tuple((m.get("computed_geometry") or m["geometry"])["coordinates"][:2])
        for m in meta
    ]
    best_start = best_len = run_start = 0
    for i in range(1, len(points)):
        if _haversine_m(points[i - 1], points[i]) > max_gap_m:
            run_start = i
        if i - run_start + 1 > best_len:
            best_len = i - run_start + 1
            best_start = run_start
    kept = meta[best_start : best_start + best_len]

    def download(indexed) -> str | None:
        index, item = indexed
        path = images_dir / f"{index:04d}.jpg"
        if path.exists() and path.stat().st_size > 5000:
            return path.name
        try:
            data = requests.get(item[IMAGE_FIELD], timeout=120).content
            if len(data) < 5000:
                return None
            path.write_bytes(data)
            return path.name
        except Exception:  # noqa: BLE001
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        names = list(pool.map(download, enumerate(kept)))

    saved = [n for n in names if n]
    poses = [
        {
            "file": f"{i:04d}.jpg",
            "id": item["id"],
            "lon": (item.get("computed_geometry") or item["geometry"])["coordinates"][0],
            "lat": (item.get("computed_geometry") or item["geometry"])["coordinates"][1],
            "compass": item.get("compass_angle"),
            "captured_at": item.get("captured_at"),
            "camera_type": item.get("camera_type"),
            "width": item.get("width"),
            "height": item.get("height"),
        }
        for i, item in enumerate(kept)
        if names[i]
    ]
    (out_dir / "poses.json").write_text(json.dumps(poses, indent=1), encoding="utf-8")

    span = sum(
        _haversine_m(
            (poses[i - 1]["lon"], poses[i - 1]["lat"]),
            (poses[i]["lon"], poses[i]["lat"]),
        )
        for i in range(1, len(poses))
    )
    summary = {
        "sequence_id": sequence_id,
        "label": label,
        "frames_downloaded": len(saved),
        "frames_in_sequence": len(meta),
        "run_span_m": round(span, 1),
        "mean_spacing_m": round(span / max(len(poses) - 1, 1), 2),
        "dir": str(out_dir),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return summary
