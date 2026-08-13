"""Ingest a self-captured multi-pass corridor into the reconstruction pipeline.

Written before the capture, deliberately. Sorting 200 photographs into passes
after the fact is guesswork; recording which pass a frame came from at ingest
time is free.

Expected layout -- one directory per pass:

    data/reality/<scene>/raw/
        pass1_forward/   IMG_0001.jpg ...
        pass2_reverse/   ...
        pass3_lateral/   ...
        pass4_sideroad/  ...
        pass5_junction/  ...

Frames are renamed into a single flat `images/` directory in pass order, with
zero-padded names, because COLMAP infers neighbour relationships from filename
order. Provenance (original name, pass, EXIF time and GPS) is preserved in
poses.json, which is the same format align.py already consumes -- so the Sim(3)
scoring works on a self-capture with no changes.

Why the reverse pass matters most: it revisits surfaces already seen on the
forward pass, which is what gives COLMAP loop-closure constraints. That is the
mechanism that attacks the 74 m scale drift, and it is unavailable from any
single-direction drive.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

# Canonical pass order. Forward first so the primary trajectory anchors the
# reconstruction; the junction orbit last so its many views attach to an
# already-stable model.
PASS_ORDER = ["pass1_forward", "pass2_reverse", "pass3_lateral", "pass4_sideroad", "pass5_junction"]

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".heic", ".JPG", ".JPEG", ".PNG", ".HEIC"}


def _rational(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        try:
            return value.numerator / value.denominator
        except Exception:  # noqa: BLE001
            return 0.0


def _exif(path: Path) -> dict:
    """Pull timestamp, GPS and heading from EXIF. Absent metadata is not fatal:
    COLMAP needs only the pixels, and GPS only improves the alignment score."""
    out: dict = {}
    try:
        from PIL import Image, ExifTags

        with Image.open(path) as image:
            raw = image.getexif()
            if not raw:
                return out
            tags = {ExifTags.TAGS.get(k, k): v for k, v in raw.items()}
            out["captured_at"] = str(tags.get("DateTimeOriginal") or tags.get("DateTime") or "")

            gps_raw = raw.get_ifd(0x8825) if hasattr(raw, "get_ifd") else None
            if gps_raw:
                gps = {ExifTags.GPSTAGS.get(k, k): v for k, v in gps_raw.items()}

                def coord(key: str, ref_key: str) -> float | None:
                    if key not in gps:
                        return None
                    d, m, s = (_rational(v) for v in gps[key])
                    value = d + m / 60 + s / 3600
                    if str(gps.get(ref_key, "")).upper() in ("S", "W"):
                        value = -value
                    return value

                lat = coord("GPSLatitude", "GPSLatitudeRef")
                lon = coord("GPSLongitude", "GPSLongitudeRef")
                if lat is not None and lon is not None:
                    out["lat"], out["lon"] = lat, lon
                if "GPSImgDirection" in gps:
                    out["compass"] = _rational(gps["GPSImgDirection"])
    except Exception:  # noqa: BLE001 - metadata is a bonus, never a requirement
        pass
    return out


def ingest(scene_dir: Path, raw_dir: Path | None = None) -> dict:
    """Flatten pass directories into images/ + poses.json, preserving provenance."""
    scene_dir = Path(scene_dir)
    raw_dir = Path(raw_dir) if raw_dir else scene_dir / "raw"
    images_dir = scene_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    if not raw_dir.exists():
        raise RuntimeError(f"No raw capture at {raw_dir}. Expected pass subdirectories.")

    # Known passes first in canonical order, then anything else alphabetically,
    # so an unplanned extra pass is still ingested rather than silently dropped.
    found = [d for d in raw_dir.iterdir() if d.is_dir()]
    ordered = [d for name in PASS_ORDER for d in found if d.name == name]
    ordered += sorted(d for d in found if d not in ordered)
    if not ordered:
        raise RuntimeError(f"No pass subdirectories inside {raw_dir}")

    poses: list[dict] = []
    counts: dict[str, int] = {}
    index = 0
    with_gps = 0

    for pass_dir in ordered:
        frames = sorted(
            p for p in pass_dir.iterdir() if p.suffix in IMAGE_SUFFIXES and p.is_file()
        )
        counts[pass_dir.name] = len(frames)
        for frame in frames:
            target = images_dir / f"{index:04d}.jpg"
            if not target.exists():
                shutil.copy2(frame, target)
            meta = _exif(frame)
            if "lat" in meta:
                with_gps += 1
            poses.append(
                {
                    "file": target.name,
                    "original": frame.name,
                    "pass": pass_dir.name,
                    "lat": meta.get("lat"),
                    "lon": meta.get("lon"),
                    "compass": meta.get("compass"),
                    "captured_at": meta.get("captured_at"),
                }
            )
            index += 1

    (scene_dir / "poses.json").write_text(json.dumps(poses, indent=1), encoding="utf-8")

    summary = {
        "scene": scene_dir.name,
        "frames": index,
        "passes": counts,
        "frames_with_gps": with_gps,
        "gps_coverage_pct": round(100 * with_gps / max(index, 1), 1),
        "images_dir": str(images_dir),
        # Loop closure is the point of the reverse pass; flag its absence loudly.
        "has_reverse_pass": counts.get("pass2_reverse", 0) > 0,
    }
    (scene_dir / "ingest.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return summary


def report(summary: dict) -> str:
    lines = [f"INGESTED {summary['frames']} frames into {summary['scene']}"]
    for name, count in summary["passes"].items():
        lines.append(f"  {name:18} {count:4d}")
    lines.append(f"  GPS coverage      {summary['gps_coverage_pct']}%")
    if not summary["has_reverse_pass"]:
        lines.append(
            "  WARNING: no reverse pass. Loop closure is what fixes scale drift; "
            "without it expect the Sim(3) alignment to fail again."
        )
    return "\n".join(lines)
