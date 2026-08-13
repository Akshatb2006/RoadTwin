"""Go/no-go reconnaissance for the photorealistic corridor.

Photogrammetry fails on thin or discontinuous imagery, and it fails *slowly* --
COLMAP will happily spend an hour proving that 87 photographs cannot become a
street. This module answers "is there enough imagery here?" in about a minute,
before any reconstruction is attempted.

The thresholds below are the decision, stated up front so the answer cannot be
rationalised after seeing the numbers:

    GO         >= 400 usable frames, >= 800 m continuous, median spacing <= 12 m
    MARGINAL   >= 150 usable frames, >= 400 m continuous
    NO-GO      anything less -- keep the procedural 3D layer and spend the time
               elsewhere

Requires a Mapillary access token (free): https://www.mapillary.com/dashboard/developers
-> create an application -> copy the client token (starts "MLY|").
Pass it as MAPILLARY_TOKEN in the environment.
"""

from __future__ import annotations

import datetime
import math
import os
import statistics
from dataclasses import dataclass, field

import requests

GRAPH = "https://graph.mapillary.com/images"

GO_FRAMES, GO_RUN_M, GO_SPACING_M = 400, 800.0, 12.0
MARGINAL_FRAMES, MARGINAL_RUN_M = 150, 400.0

# A gap longer than this breaks a continuous run: Structure-from-Motion loses
# feature correspondence between frames much beyond it.
MAX_LINK_M = 25.0


@dataclass
class CorridorReport:
    images: int = 0
    sequences: int = 0
    capture_dates: int = 0
    median_spacing_m: float = 0.0
    max_gap_m: float = 0.0
    longest_run_m: float = 0.0
    longest_run_frames: int = 0
    is_pano: int = 0
    verdict: str = "NO-GO"
    reasons: list[str] = field(default_factory=list)
    best_sequence: str = ""

    def as_table(self) -> str:
        rows = [
            ("Images found", f"{self.images:,}"),
            ("Unique sequences", str(self.sequences)),
            ("Unique capture dates", str(self.capture_dates)),
            ("Median spacing", f"{self.median_spacing_m:.1f} m"),
            ("Largest gap", f"{self.max_gap_m:.0f} m"),
            ("Longest continuous run", f"{self.longest_run_m:.0f} m"),
            ("Usable frames (best run)", str(self.longest_run_frames)),
            ("360 panoramas", str(self.is_pano)),
        ]
        width = max(len(k) for k, _ in rows)
        body = "\n".join(f"  {k.ljust(width)}  {v}" for k, v in rows)
        return f"MAPILLARY CORRIDOR CHECK\n{body}\n  VERDICT: {self.verdict}"


def _haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlon, dlat = lon2 - lon1, lat2 - lat1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6_371_000 * math.asin(min(1.0, math.sqrt(h)))


def fetch_images(bbox: dict, token: str, limit: int = 2000) -> list[dict]:
    """Query the Mapillary Graph API for images inside a bbox."""
    params = {
        "fields": "id,computed_geometry,geometry,sequence,captured_at,is_pano,compass_angle",
        "bbox": f"{bbox['west']},{bbox['south']},{bbox['east']},{bbox['north']}",
        "limit": limit,
        "access_token": token,
    }
    response = requests.get(GRAPH, params=params, timeout=120)
    if response.status_code != 200:
        raise RuntimeError(f"Mapillary HTTP {response.status_code}: {response.text[:300]}")
    return response.json().get("data", [])


def analyse(images: list[dict]) -> CorridorReport:
    """Turn raw image metadata into a go/no-go decision."""
    report = CorridorReport(images=len(images))
    if not images:
        report.reasons.append("No imagery returned for this corridor.")
        return report

    by_sequence: dict[str, list[dict]] = {}
    dates: set[str] = set()
    for image in images:
        by_sequence.setdefault(image.get("sequence", "?"), []).append(image)
        captured = image.get("captured_at")
        if captured:
            # captured_at is epoch MILLISECONDS; slicing the digits counts
            # timestamps, not days, and reports thousands of "dates".
            dates.add(
                datetime.datetime.fromtimestamp(
                    int(captured) / 1000, datetime.UTC
                ).strftime("%Y-%m-%d")
            )
        if image.get("is_pano"):
            report.is_pano += 1

    report.sequences = len(by_sequence)
    report.capture_dates = len(dates)

    all_spacings: list[float] = []
    for sequence_id, frames in by_sequence.items():
        frames.sort(key=lambda f: f.get("captured_at") or 0)
        points = []
        for frame in frames:
            geo = frame.get("computed_geometry") or frame.get("geometry")
            if geo and geo.get("coordinates"):
                points.append(tuple(geo["coordinates"][:2]))
        if len(points) < 2:
            continue

        # Walk the sequence, breaking it wherever the gap is too large for SfM.
        run_length, run_frames = 0.0, 1
        for i in range(1, len(points)):
            step = _haversine_m(points[i - 1], points[i])
            all_spacings.append(step)
            report.max_gap_m = max(report.max_gap_m, step)
            if step <= MAX_LINK_M:
                run_length += step
                run_frames += 1
            else:
                run_length, run_frames = 0.0, 1
            # Track the best run by FRAME COUNT, not by distance. Ranking by
            # distance picks a long sparse drive over a shorter dense one, and
            # frames -- not metres -- are what a reconstruction actually needs.
            if run_frames > report.longest_run_frames:
                report.longest_run_frames = run_frames
                report.longest_run_m = run_length
                report.best_sequence = sequence_id

    if all_spacings:
        report.median_spacing_m = statistics.median(all_spacings)

    # ---- verdict ----
    if (
        report.longest_run_frames >= GO_FRAMES
        and report.longest_run_m >= GO_RUN_M
        and report.median_spacing_m <= GO_SPACING_M
    ):
        report.verdict = "GO"
        report.reasons.append(
            f"{report.longest_run_frames} continuous frames over "
            f"{report.longest_run_m:.0f} m at {report.median_spacing_m:.1f} m spacing."
        )
    elif report.longest_run_frames >= MARGINAL_FRAMES and report.longest_run_m >= MARGINAL_RUN_M:
        report.verdict = "MARGINAL"
        report.reasons.append(
            "Enough imagery for a short reconstruction, not a full corridor. "
            "Attempt only if the rest of the demo is finished."
        )
    else:
        report.verdict = "NO-GO"
        report.reasons.append(
            f"Longest continuous run is {report.longest_run_frames} frames over "
            f"{report.longest_run_m:.0f} m. Reconstruction would be a stitched "
            "fragment, not a corridor."
        )
    if report.capture_dates > 1:
        report.reasons.append(
            f"Imagery spans {report.capture_dates} capture dates; mixing them "
            "introduces lighting and scene changes that hurt reconstruction."
        )
    return report


def scout(bbox: dict, token: str | None = None) -> CorridorReport:
    token = token or os.environ.get("MAPILLARY_TOKEN", "")
    if not token:
        raise RuntimeError(
            "No Mapillary token. Get one free at "
            "https://www.mapillary.com/dashboard/developers and set MAPILLARY_TOKEN."
        )
    return analyse(fetch_images(bbox, token))
