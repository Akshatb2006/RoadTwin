"""Prune a trained Gaussian splat into something presentable.

3DGS in a weakly-constrained scene produces three characteristic artifacts, all
visible in the raw 80 Feet Road reconstruction:

  * OVERSIZED Gaussians. Median linear scale is 0.054; the maximum is 19.87 --
    roughly 370x. Where the optimiser could not resolve a surface it stretched
    one blob across the view. These are the white smears.
  * NEAR-TRANSPARENT Gaussians. 36k splats sit below 0.15 opacity. Individually
    invisible, collectively a grey haze over everything.
  * FAR OUTLIERS. A tail of splats up to 55 units from the corridor centre,
    where the median is 5.6 -- floaters behind the camera and in the sky.

None of this is fixable by training longer: the information is not in the
imagery. It IS fixable by deletion, because these Gaussians carry almost no
real signal. Pruning is lossy and honest -- we remove what the reconstruction
could not resolve rather than pretending it resolved it.
"""

from __future__ import annotations

import numpy as np
from pathlib import Path
from plyfile import PlyData, PlyElement


def prune(
    src: Path,
    dst: Path,
    scale_percentile: float = 97.0,
    min_opacity: float = 0.15,
    distance_percentile: float = 99.0,
) -> dict:
    """Delete oversized, transparent and distant Gaussians. Returns a summary."""
    ply = PlyData.read(str(src))
    vertex = ply["vertex"]
    data = vertex.data
    total = len(data)

    scales = np.exp(np.stack([vertex["scale_0"], vertex["scale_1"], vertex["scale_2"]], 1))
    largest_axis = scales.max(axis=1)
    opacity = 1.0 / (1.0 + np.exp(-np.asarray(vertex["opacity"])))

    xyz = np.stack([vertex["x"], vertex["y"], vertex["z"]], 1)
    centre = np.median(xyz, axis=0)
    distance = np.linalg.norm(xyz - centre, axis=1)

    scale_limit = np.percentile(largest_axis, scale_percentile)
    distance_limit = np.percentile(distance, distance_percentile)

    keep = (
        (largest_axis <= scale_limit)
        & (opacity >= min_opacity)
        & (distance <= distance_limit)
    )

    kept = data[keep]
    PlyData([PlyElement.describe(kept, "vertex")], text=False).write(str(dst))

    return {
        "source_splats": int(total),
        "kept_splats": int(keep.sum()),
        "removed_splats": int(total - keep.sum()),
        "removed_pct": round(100.0 * (1 - keep.mean()), 1),
        "removed_oversized": int((largest_axis > scale_limit).sum()),
        "removed_transparent": int((opacity < min_opacity).sum()),
        "removed_distant": int((distance > distance_limit).sum()),
        "scale_limit": round(float(scale_limit), 4),
        "output": str(dst),
        "output_mb": round(dst.stat().st_size / 1e6, 1),
    }
