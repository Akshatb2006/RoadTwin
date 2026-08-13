"""Traffic demand generation.

Routes are generated with SUMO's `randomTrips.py` + `duarouter`, driven with a
fringe bias so that traffic realistically enters and leaves the study area
rather than circulating inside it.

Important design point: generated routes reference the vehicle type by the
*distribution name* ("indian_mix"), never by a concrete type. The actual modal
split, lane discipline and weather are resolved at simulation time from a
separate additional-file. That means one expensive routing pass can be reused
across every scenario variant -- which is what makes side-by-side comparison
fast enough to demo live.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from ..config import CACHE_DIR, SUMO_TOOLS

RANDOM_TRIPS = SUMO_TOOLS / "randomTrips.py"


def demand_key(net_path: Path, duration_s: int, demand_multiplier: float, seed: int) -> str:
    raw = f"{net_path.name}|{duration_s}|{demand_multiplier:.3f}|{seed}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def insertion_period(lane_km: float, demand_multiplier: float) -> float:
    """Seconds between vehicle insertions, scaled to network size.

    Calibrated so a typical Indian urban arterial network runs at a busy but
    pre-saturation flow at multiplier 1.0, leaving headroom for the "+30% peak"
    scenario to actually show degradation instead of instant gridlock.
    """
    veh_per_sec = max(0.25, min(3.0, lane_km * 0.018)) * max(demand_multiplier, 0.01)
    return 1.0 / veh_per_sec


def generate_routes(
    net_path: Path,
    lane_km: float,
    duration_s: int,
    demand_multiplier: float = 1.0,
    seed: int = 42,
    force: bool = False,
) -> Path:
    """Produce a .rou.xml of validated routes. Cached on disk by parameters."""
    key = demand_key(net_path, duration_s, demand_multiplier, seed)
    routes_path = CACHE_DIR / f"routes_{key}.rou.xml"
    if _usable(routes_path) and not force:
        return routes_path

    # Parallel scenario sweeps routinely need identical demand (e.g. comparing
    # four signal strategies at +40%). Without this lock, several workers run
    # randomTrips into the same path at once and corrupt each other's output.
    with _exclusive_lock(routes_path):
        if _usable(routes_path) and not force:
            return routes_path
        return _generate_locked(
            net_path, routes_path, key, lane_km, duration_s, demand_multiplier, seed
        )


def _usable(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 512


@contextmanager
def _exclusive_lock(target: Path):
    lock_path = target.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _generate_locked(
    net_path: Path,
    routes_path: Path,
    key: str,
    lane_km: float,
    duration_s: int,
    demand_multiplier: float,
    seed: int,
) -> Path:
    # Build into process-unique temp paths, then rename atomically, so a reader
    # never observes a half-written routes file.
    unique = f"{key}_{os.getpid()}"
    trips_path = CACHE_DIR / f"trips_{unique}.trips.xml"
    tmp_routes = CACHE_DIR / f"routes_{unique}.tmp.rou.xml"
    period = insertion_period(lane_km, demand_multiplier)

    cmd = [
        sys.executable,
        str(RANDOM_TRIPS),
        "-n", str(net_path),
        "-o", str(trips_path),
        "-r", str(tmp_routes),
        "-b", "0",
        "-e", str(duration_s),
        "-p", f"{period:.4f}",
        # Bias trip ends toward the network fringe: models through-traffic,
        # which is what actually loads an urban corridor.
        "--fringe-factor", "8",
        "--min-distance", "250",
        # Concentrate demand onto real corridors. Without these, randomTrips
        # samples edges uniformly, so a residential service lane attracts as
        # much traffic as an arterial -- demand never concentrates anywhere and
        # the network cannot be congested no matter how high the volume goes.
        "--lanes",              # weight by lane count
        "--speed-exponent", "2.5",  # strongly favour higher-speed roads
        "--length",             # weight by edge length
        "--seed", str(seed),
        "--validate",
        "--remove-loops",
        "--trip-attributes",
        'type="indian_mix" departLane="best" departSpeed="max"',
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if not tmp_routes.exists() or tmp_routes.stat().st_size < 200:
        raise RuntimeError(
            f"randomTrips failed (exit {result.returncode}).\n"
            f"stdout:\n{result.stdout[-1500:]}\nstderr:\n{result.stderr[-2500:]}"
        )
    os.replace(tmp_routes, routes_path)   # atomic publish
    trips_path.unlink(missing_ok=True)
    return routes_path
