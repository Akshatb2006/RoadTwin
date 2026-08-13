"""Network variants for capacity-increasing interventions.

A lane closure can be applied to a running simulation over TraCI. A lane
*addition* cannot: lane count is part of the network geometry, fixed when
netconvert builds it. Faking it (inflating a speed limit, or scaling capacity
after the fact) would produce a number that looks like a widened road without
simulating one.

Instead we patch the network properly. netconvert can re-read a built .net.xml
together with a plain-XML edge patch, so adding a lane re-derives the junction
connections, turn lanes and right-of-way around it -- the same treatment the
original build got. The variant is cached, so an intervention is built once and
reused across every scenario that includes it.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from ..config import CACHE_DIR, NETCONVERT
from ..contracts import LaneAddition, RoadNetwork


def variant_key(additions: list[LaneAddition]) -> str:
    parts = sorted(f"{a.segment_id}:{a.lanes_added}" for a in additions)
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:10]


def build_widened_network(
    network: RoadNetwork, additions: list[LaneAddition]
) -> tuple[Path, dict[str, tuple[int, int]]]:
    """Return a .net.xml with the requested segments widened.

    Also returns {segment_id: (lanes_before, lanes_after)} so the UI can state
    exactly what was changed rather than just asserting "a lane was added".
    """
    base = Path(network.sumo_net_path)
    lanes_by_id = {s.id: s.lanes for s in network.segments}

    applied: dict[str, tuple[int, int]] = {}
    for addition in additions:
        before = lanes_by_id.get(addition.segment_id)
        if before is None:
            continue
        after = max(1, before + addition.lanes_added)
        applied[addition.segment_id] = (before, after)

    if not applied:
        return base, {}

    out_path = CACHE_DIR / f"{network.id}_wide_{variant_key(additions)}.net.xml"
    if out_path.exists() and out_path.stat().st_size > 1024:
        return out_path, applied

    patch = CACHE_DIR / f"patch_{network.id}_{variant_key(additions)}.edg.xml"
    rows = "\n".join(
        f'  <edge id="{eid}" numLanes="{after}"/>' for eid, (_b, after) in applied.items()
    )
    patch.write_text(
        f'<?xml version="1.0" encoding="UTF-8"?>\n<edges>\n{rows}\n</edges>\n',
        encoding="utf-8",
    )

    # NOTE: --lefthand is deliberately NOT repeated here. The source network was
    # already built left-hand; passing it again would mirror the geometry twice.
    result = subprocess.run(
        [
            NETCONVERT,
            "--sumo-net-file", str(base),
            "--edge-files", str(patch),
            "--output-file", str(out_path),
            "--no-warnings", "true",
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if not out_path.exists() or out_path.stat().st_size < 1024:
        raise RuntimeError(
            f"netconvert could not widen the network (exit {result.returncode}).\n"
            f"{result.stderr[-1500:]}"
        )
    return out_path, applied
