"""COLMAP Structure-from-Motion over a Mapillary sequence.

We need only the SPARSE reconstruction: camera intrinsics, per-frame poses and
a sparse point cloud. That is exactly what Gaussian Splatting consumes as
initialisation, and -- importantly on this machine -- sparse SfM is the part of
COLMAP that does not require CUDA. Dense MVS does, and we never run it.

Two choices matter for a dashcam-style capture:

* SEQUENTIAL matching, not exhaustive. Frames arrive in capture order, so image
  N only needs comparing against its neighbours. Exhaustive matching on 152
  frames is 11,476 pairs against roughly 1,500 for sequential -- hours versus
  minutes, for a worse result.
* SIMPLE_RADIAL with a shared intrinsic. Every frame comes from the same
  physical camera on the same drive, so solving one intrinsic across all frames
  is both faster and better-conditioned than solving 152 of them.

Loop detection is enabled because a corridor driven in both directions gives
revisits that dramatically stabilise the reconstruction.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

COLMAP = shutil.which("colmap") or "colmap"


def _run(args: list[str], log: Path) -> tuple[int, str]:
    with open(log, "a") as handle:
        handle.write(f"\n\n$ {' '.join(args)}\n")
        handle.flush()
        process = subprocess.run(args, stdout=handle, stderr=subprocess.STDOUT, text=True)
    tail = log.read_text(errors="ignore")[-1500:]
    return process.returncode, tail


def reconstruct(
    work_dir: Path,
    image_list: list[str] | None = None,
    max_image_size: int = 1600,
    use_gpu: bool = False,
) -> dict:
    """Run sparse SfM. Returns a summary dict; raises only on hard failure."""
    work_dir = Path(work_dir)
    images = work_dir / "images"
    sparse = work_dir / "sparse"
    database = work_dir / "database.db"
    log = work_dir / "colmap.log"
    sparse.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()

    list_path = None
    if image_list:
        list_path = work_dir / "image_list.txt"
        list_path.write_text("\n".join(sorted(image_list)), encoding="utf-8")

    # ---- 1. features ----
    extract = [
        COLMAP, "feature_extractor",
        "--database_path", str(database),
        "--image_path", str(images),
        "--ImageReader.single_camera", "1",
        "--ImageReader.camera_model", "SIMPLE_RADIAL",
        "--FeatureExtraction.use_gpu", "1" if use_gpu else "0",
        "--FeatureExtraction.max_image_size", str(max_image_size),
    ]
    if list_path:
        extract += ["--image_list_path", str(list_path)]
    code, tail = _run(extract, log)
    if code != 0:
        raise RuntimeError(f"feature_extractor failed ({code}):\n{tail}")

    # ---- 2. matching ----
    match = [
        COLMAP, "sequential_matcher",
        "--database_path", str(database),
        "--FeatureMatching.use_gpu", "1" if use_gpu else "0",
        "--SequentialMatching.overlap", "12",
        "--SequentialMatching.quadratic_overlap", "1",
        "--SequentialMatching.loop_detection", "0",
    ]
    code, tail = _run(match, log)
    if code != 0:
        raise RuntimeError(f"sequential_matcher failed ({code}):\n{tail}")

    # ---- 3. incremental mapping ----
    mapper = [
        COLMAP, "mapper",
        "--database_path", str(database),
        "--image_path", str(images),
        "--output_path", str(sparse),
        "--Mapper.ba_global_function_tolerance", "1e-5",
        "--Mapper.multiple_models", "0",
    ]
    if list_path:
        mapper += ["--Mapper.image_list_path", str(list_path)]
    code, tail = _run(mapper, log)
    if code != 0:
        raise RuntimeError(f"mapper failed ({code}):\n{tail}")

    return summarise(work_dir, round(time.perf_counter() - started, 1))


def summarise(work_dir: Path, seconds: float = 0.0) -> dict:
    """Read the reconstruction back and report whether it is usable."""
    model = Path(work_dir) / "sparse" / "0"
    if not model.exists():
        return {"ok": False, "error": "no model produced", "seconds": seconds}

    text_dir = Path(work_dir) / "sparse_text"
    text_dir.mkdir(exist_ok=True)
    subprocess.run(
        [COLMAP, "model_converter", "--input_path", str(model),
         "--output_path", str(text_dir), "--output_type", "TXT"],
        capture_output=True, text=True,
    )

    registered = 0
    images_txt = text_dir / "images.txt"
    if images_txt.exists():
        registered = sum(
            1
            for line in images_txt.read_text(errors="ignore").splitlines()
            if line and not line.startswith("#") and len(line.split()) > 8
        ) // 1

    points = 0
    points_txt = text_dir / "points3D.txt"
    if points_txt.exists():
        points = sum(
            1
            for line in points_txt.read_text(errors="ignore").splitlines()
            if line and not line.startswith("#")
        )

    summary = {
        "ok": registered > 0 and points > 0,
        "registered_images": registered,
        "sparse_points": points,
        "seconds": seconds,
        "model_dir": str(model),
    }
    (Path(work_dir) / "reconstruction.json").write_text(
        json.dumps(summary, indent=1), encoding="utf-8"
    )
    return summary
