"""COLMAP -> RoadTwin alignment via Sim(3), solved from GPS correspondences.

Structure-from-Motion reconstructs a scene up to an unknown similarity: the
result has arbitrary scale, rotation and origin. To place SUMO vehicles inside
the reconstruction we need the seven parameters that carry one frame into the
other (1 scale + 3 rotation + 3 translation).

We already hold the correspondences. Every harvested frame has a Mapillary GPS
position, and COLMAP recovered a camera centre for the same frame. Matching them
by filename gives 44 point pairs, which is far more than the 3 needed -- so the
transform is over-determined and its residuals are meaningful.

Solved in closed form with Umeyama (1991), not by iterative fitting, and
reported as median / P95 / max error in metres. The alignment is therefore
measured rather than adjusted until it looks right: a bad transform shows up as
a large residual instead of as a subtly wrong scene that nobody notices until
vehicles are floating above the road.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

import numpy as np
from pyproj import Transformer


def umeyama(source: np.ndarray, target: np.ndarray) -> tuple[float, np.ndarray, np.ndarray]:
    """Least-squares similarity transform mapping `source` onto `target`.

    Returns (scale, R, t) such that  target ~= scale * R @ source + t.
    """
    n = source.shape[0]
    mu_src = source.mean(axis=0)
    mu_tgt = target.mean(axis=0)
    src_c = source - mu_src
    tgt_c = target - mu_tgt

    covariance = (tgt_c.T @ src_c) / n
    U, D, Vt = np.linalg.svd(covariance)

    # Guard against a reflection: without this the fit can mirror the scene,
    # which produces a plausible-looking residual and a mirrored street.
    S = np.eye(3)
    if np.linalg.det(U) * np.linalg.det(Vt) < 0:
        S[2, 2] = -1.0

    R = U @ S @ Vt
    variance = (src_c ** 2).sum() / n
    scale = float(np.trace(np.diag(D) @ S) / variance) if variance > 0 else 1.0
    t = mu_tgt - scale * R @ mu_src
    return scale, R, t


def align_scene(scene_dir: Path, poses_dir: Path | None = None) -> dict:
    """Solve and score the COLMAP -> local-metric transform for a scene."""
    scene_dir = Path(scene_dir)
    cameras = json.loads((scene_dir / "cameras.json").read_text(encoding="utf-8"))
    poses_path = (poses_dir or scene_dir) / "poses.json"
    if not poses_path.exists():
        poses_path = scene_dir.parent / "seq2016_dense" / "poses.json"
    gps = {p["file"]: p for p in json.loads(poses_path.read_text(encoding="utf-8"))}

    pairs = [(c, gps[c["name"]]) for c in cameras["cameras"] if c["name"] in gps]
    if len(pairs) < 4:
        return {"ok": False, "error": f"only {len(pairs)} correspondences"}

    # Work in a corridor-local metric frame (UTM, origin at the corridor centre)
    # rather than attempting global georeferencing, which this data cannot support.
    lons = [g["lon"] for _c, g in pairs]
    lats = [g["lat"] for _c, g in pairs]
    zone = int((sum(lons) / len(lons) + 180) // 6) + 1
    epsg = 32600 + zone if sum(lats) / len(lats) >= 0 else 32700 + zone
    to_utm = Transformer.from_crs(4326, epsg, always_xy=True)

    utm = np.array([to_utm.transform(g["lon"], g["lat"]) for _c, g in pairs])
    origin = utm.mean(axis=0)
    target = np.column_stack([utm[:, 0] - origin[0], utm[:, 1] - origin[1], np.zeros(len(pairs))])
    source = np.array([c["C"] for c, _g in pairs])

    scale, R, t = umeyama(source, target)
    predicted = (scale * (R @ source.T).T) + t
    # GPS has no usable altitude here, so score horizontal error only.
    residuals = np.linalg.norm(predicted[:, :2] - target[:, :2], axis=1)

    result = {
        "ok": True,
        "correspondences": len(pairs),
        "transform": "Sim(3)",
        "epsg": epsg,
        "origin_utm": origin.tolist(),
        "scale": round(scale, 6),
        "rotation": R.tolist(),
        "translation": t.tolist(),
        "median_error_m": round(float(statistics.median(residuals)), 2),
        "p95_error_m": round(float(np.percentile(residuals, 95)), 2),
        "max_error_m": round(float(residuals.max()), 2),
        "rmse_m": round(float(np.sqrt((residuals ** 2).mean())), 2),
        # Scale converts COLMAP units to metres; the corridor length is a
        # sanity check that the fit is physically plausible.
        "corridor_length_m": round(
            float(np.linalg.norm(target[-1, :2] - target[0, :2])), 1
        ),
    }
    (scene_dir / "alignment.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    return result


def report(result: dict) -> str:
    if not result.get("ok"):
        return f"ALIGNMENT FAILED: {result.get('error')}"
    rows = [
        ("Correspondences", str(result["correspondences"])),
        ("Transform", result["transform"]),
        ("Scale (COLMAP->m)", f"{result['scale']:.4f}"),
        ("Median error", f"{result['median_error_m']} m"),
        ("P95 error", f"{result['p95_error_m']} m"),
        ("Maximum error", f"{result['max_error_m']} m"),
        ("RMSE", f"{result['rmse_m']} m"),
        ("Corridor length", f"{result['corridor_length_m']} m"),
    ]
    width = max(len(k) for k, _ in rows)
    body = "\n".join(f"  {k.ljust(width)}  {v}" for k, v in rows)
    return f"REALITY -> ROADTWIN ALIGNMENT\n{body}"
