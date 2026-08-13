"""Paths, SUMO discovery, and location presets."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .contracts import BBox

# ---------------------------------------------------------------- paths
BACKEND_DIR = Path(__file__).resolve().parent.parent
ROOT_DIR = BACKEND_DIR.parent
DATA_DIR = ROOT_DIR / "data"
CACHE_DIR = DATA_DIR / "cache"      # raw .osm downloads + built networks
RUNS_DIR = DATA_DIR / "runs"        # per-simulation working dirs

for _d in (DATA_DIR, CACHE_DIR, RUNS_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- SUMO
def _discover_sumo_home() -> Path:
    """Locate SUMO. The pip `eclipse-sumo` wheel installs it under site-packages."""
    if os.environ.get("SUMO_HOME"):
        p = Path(os.environ["SUMO_HOME"])
        if p.exists():
            return p
    try:
        import sumolib  # noqa: F401

        # sumolib lives in site-packages/sumolib, binaries in site-packages/sumo
        site_packages = Path(sumolib.__file__).resolve().parent.parent
        candidate = site_packages / "sumo"
        if candidate.exists():
            return candidate
    except ImportError:
        pass
    raise RuntimeError(
        "Could not locate SUMO. Install with `pip install eclipse-sumo` "
        "or set the SUMO_HOME environment variable."
    )


SUMO_HOME = _discover_sumo_home()
os.environ["SUMO_HOME"] = str(SUMO_HOME)

SUMO_TOOLS = SUMO_HOME / "tools"
if SUMO_TOOLS.exists() and str(SUMO_TOOLS) not in sys.path:
    sys.path.insert(0, str(SUMO_TOOLS))


def sumo_bin(name: str) -> str:
    """Resolve a SUMO executable, preferring the venv's bin dir."""
    venv_bin = Path(sys.executable).parent / name
    if venv_bin.exists():
        return str(venv_bin)
    packaged = SUMO_HOME / "bin" / name
    if packaged.exists():
        return str(packaged)
    return name  # fall back to PATH


NETCONVERT = sumo_bin("netconvert")
DUAROUTER = sumo_bin("duarouter")
SUMO_BINARY = sumo_bin("sumo")
POLYCONVERT = sumo_bin("polyconvert")


# ---------------------------------------------------------------- presets
# Curated Indian locations sized for a hackathon demo: each is a real,
# genuinely congested area, small enough to build and simulate in seconds.
PRESETS: dict[str, dict] = {
    "koramangala": {
        "name": "Koramangala, Bengaluru",
        "description": "Dense grid, Sony World & Forum junctions, heavy 2W mix",
        "bbox": BBox(south=12.9280, west=77.6180, north=12.9425, east=77.6350),
    },
    "connaught_place": {
        "name": "Connaught Place, New Delhi",
        "description": "Radial roundabout system, high bus and auto share",
        "bbox": BBox(south=28.6280, west=77.2130, north=28.6395, east=77.2280),
    },
    "hitech_city": {
        "name": "HITEC City, Hyderabad",
        "description": "IT corridor arterials with severe peak-hour tidal flow",
        "bbox": BBox(south=17.4380, west=78.3760, north=17.4520, east=78.3920),
    },
    "andheri_east": {
        "name": "Andheri East, Mumbai",
        "description": "Western Express Highway interface, mixed freight and 2W",
        "bbox": BBox(south=19.1080, west=72.8560, north=19.1215, east=72.8720),
    },
    "anna_salai": {
        "name": "Anna Salai, Chennai",
        "description": "Primary arterial spine with signalised corridor",
        "bbox": BBox(south=13.0530, west=80.2450, north=13.0670, east=80.2600),
    },
    "iit_bombay": {
        "name": "Powai / IIT Bombay, Mumbai",
        "description": "Compact campus-edge network, quick to build (demo-safe)",
        "bbox": BBox(south=19.1250, west=72.9080, north=19.1360, east=72.9200),
    },
}
