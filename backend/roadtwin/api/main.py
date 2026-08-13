"""RoadTwin HTTP API."""

from __future__ import annotations

import json
import time
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from ..ai.planner import parse_scenario
from ..config import PRESETS
from ..contracts import (
    BBox,
    Comparison,
    Experiment,
    InterventionResult,
    LaneAddition,
    LaneClosure,
    RoadNetwork,
    RunStatus,
    Scenario,
    SignalStrategy,
    SimulationRun,
)
from ..enrich.indian import enrich_network, network_summary
from ..osm.build import build_network
from ..osm.buildings import fetch_buildings
from ..sim.analysis import compare_metrics, recommend_intervention
from . import store

app = FastAPI(
    title="RoadTwin API",
    version="1.0.0",
    description="Automated high-fidelity road network modelling for Indian traffic simulation",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------------ requests


class BuildRequest(BaseModel):
    preset: str | None = None
    bbox: BBox | None = None
    name: str | None = None
    force: bool = False


class RunRequest(BaseModel):
    network_id: str
    scenario: Scenario


class ParseRequest(BaseModel):
    network_id: str
    prompt: str = Field(min_length=1, max_length=1000)


class SweepRequest(BaseModel):
    """Run many scenarios at once across the process pool."""

    network_id: str
    scenarios: list[Scenario] = Field(min_length=1, max_length=24)


class ExperimentRequest(BaseModel):
    """A control plus the interventions to test against it, at one demand."""

    network_id: str
    base_scenario: Scenario
    interventions: list[str] = Field(default_factory=list, max_length=6)
    target_segments: list[str] = Field(default_factory=list, max_length=8)


class CompareRequest(BaseModel):
    baseline_run_id: str
    scenario_run_id: str


# -------------------------------------------------------------------- routes


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "workers": store.MAX_WORKERS,
        "networks_cached": len(store.list_networks()),
    }


@app.get("/api/presets")
def get_presets() -> list[dict]:
    return [
        {
            "key": key,
            "name": value["name"],
            "description": value["description"],
            "bbox": value["bbox"].model_dump(),
            "center": value["bbox"].center.model_dump(),
        }
        for key, value in PRESETS.items()
    ]


@app.post("/api/networks")
def create_network(request: BuildRequest) -> dict:
    """Build a simulation-ready digital twin. The headline operation."""
    started = time.perf_counter()

    if request.preset:
        preset = PRESETS.get(request.preset)
        if not preset:
            raise HTTPException(404, f"Unknown preset '{request.preset}'")
        bbox, name = preset["bbox"], request.name or preset["name"]
        network_id = f"net_{request.preset}"
    elif request.bbox:
        bbox = request.bbox
        name = request.name or "Custom area"
        network_id = None
    else:
        raise HTTPException(400, "Provide either a preset key or a bbox")

    existing = store.load_network(network_id) if network_id else None
    if existing and not request.force:
        return {
            "network": _network_response(existing),
            "cached": True,
            "build_seconds": existing.stats.build_seconds,
        }

    try:
        network = build_network(bbox, name, force=request.force, network_id=network_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Network build failed: {exc}") from exc

    enrich_network(network)
    network.stats.build_seconds = round(time.perf_counter() - started, 2)
    store.save_network(network)

    return {
        "network": _network_response(network),
        "cached": False,
        "build_seconds": network.stats.build_seconds,
    }


@app.get("/api/networks")
def get_networks() -> list[dict]:
    return [
        {"id": n.id, "name": n.name, "stats": n.stats.model_dump(), "bbox": n.bbox.model_dump()}
        for n in store.list_networks()
    ]


@app.get("/api/networks/{network_id}")
def get_network(network_id: str) -> dict:
    network = store.load_network(network_id)
    if not network:
        raise HTTPException(404, "Network not found")
    return _network_response(network)


@app.get("/api/networks/{network_id}/geometry")
def get_network_geometry(network_id: str) -> JSONResponse:
    """GeoJSON of the road graph -- this is what the map renders."""
    network = store.load_network(network_id)
    if not network:
        raise HTTPException(404, "Network not found")

    features = [
        {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": segment.geometry},
            "properties": {
                "id": segment.id,
                "name": segment.name,
                "road_class": segment.road_class.value,
                "lanes": segment.lanes,
                "effective_lanes": segment.effective_lanes,
                "speed_limit_kmh": segment.speed_limit_kmh,
                "length_m": segment.length_m,
                "capacity_pcu_hr": segment.capacity_pcu_hr,
                "encroachment": segment.encroachment,
                "surface": segment.surface.value,
                "oneway": segment.oneway,
            },
        }
        for segment in network.segments
        if len(segment.geometry) >= 2
    ]
    junction_features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [j.lon, j.lat]},
            "properties": {"id": j.id, "type": j.type.value, "degree": j.degree},
        }
        for j in network.junctions
        if j.degree >= 3
    ]
    return JSONResponse(
        {
            "roads": {"type": "FeatureCollection", "features": features},
            "junctions": {"type": "FeatureCollection", "features": junction_features},
        }
    )


@app.get("/api/networks/{network_id}/buildings")
def get_buildings(network_id: str) -> JSONResponse:
    """Building footprints with heights -- the physical layer of the twin."""
    network = store.load_network(network_id)
    if not network:
        raise HTTPException(404, "Network not found")
    try:
        return JSONResponse(fetch_buildings(network.bbox))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Building fetch failed: {exc}") from exc


@app.post("/api/scenario/parse")
def parse(request: ParseRequest) -> dict:
    """Natural language -> validated Scenario. The LLM never runs the simulation."""
    network = store.load_network(request.network_id)
    if not network:
        raise HTTPException(404, "Network not found")
    scenario, explanation = parse_scenario(request.prompt, network)
    return {"scenario": scenario.model_dump(), "explanation": explanation}


@app.post("/api/runs")
def create_run(request: RunRequest) -> dict:
    network = store.load_network(request.network_id)
    if not network:
        raise HTTPException(404, "Network not found")
    run = store.submit_run(network, request.scenario)
    return _run_response(run)


@app.post("/api/sweep")
def create_sweep(request: SweepRequest) -> dict:
    """Fan a scenario set across the worker pool. Returns immediately."""
    network = store.load_network(request.network_id)
    if not network:
        raise HTTPException(404, "Network not found")
    runs = [
        store.submit_run(network, scenario, capture_playback=False)
        for scenario in request.scenarios
    ]
    return {
        "sweep_id": f"sweep_{uuid.uuid4().hex[:8]}",
        "run_ids": [r.id for r in runs],
        "workers": store.MAX_WORKERS,
    }


@app.post("/api/experiment")
def create_experiment(request: ExperimentRequest) -> dict:
    """Run a control plus N interventions at IDENTICAL demand, in parallel.

    This is the methodological core of the product. Comparing "today" against
    "peak demand + a lane closure" conflates two changes; comparing a control
    against each intervention at the same demand isolates the intervention, so
    the resulting statement -- "this lane reduces delay by X%" -- is defensible.
    """
    network = store.load_network(request.network_id)
    if not network:
        raise HTTPException(404, "Network not found")

    base = request.base_scenario
    targets = request.target_segments or _busiest_segments(network, 3)

    variants: list[tuple[str, str, Scenario]] = [
        ("control", "No intervention", base.model_copy(update={
            "id": f"{base.id}_control", "name": "Control",
            "lane_closures": [], "lane_additions": [], "incidents": [],
            "signal_strategy": SignalStrategy.FIXED,
        })),
    ]

    for key in request.interventions:
        if key == "add_lane":
            scenario = base.model_copy(update={
                "id": f"{base.id}_addlane", "name": "Add one lane",
                "lane_additions": [LaneAddition(segment_id=s, lanes_added=1) for s in targets],
                "lane_closures": [], "signal_strategy": SignalStrategy.FIXED,
            })
            variants.append((key, "Add one lane", scenario))
        elif key == "close_lane":
            scenario = base.model_copy(update={
                "id": f"{base.id}_closelane", "name": "Close one lane",
                "lane_closures": [LaneClosure(segment_id=s, lanes_closed=1) for s in targets],
                "lane_additions": [], "signal_strategy": SignalStrategy.FIXED,
            })
            variants.append((key, "Close one lane", scenario))
        elif key in ("adaptive", "max_pressure"):
            label = "Adaptive signals" if key == "adaptive" else "Max-pressure signals"
            scenario = base.model_copy(update={
                "id": f"{base.id}_{key}", "name": label,
                "lane_closures": [], "lane_additions": [],
                "signal_strategy": SignalStrategy(key),
            })
            variants.append((key, label, scenario))

    runs = [
        (key, label, store.submit_run(network, scenario, capture_playback=False))
        for key, label, scenario in variants
    ]

    experiment = Experiment(
        id=f"exp_{uuid.uuid4().hex[:8]}",
        network_id=network.id,
        demand_multiplier=base.demand_multiplier,
        duration_s=base.duration_s,
        control_run_id=runs[0][2].id,
        results=[
            InterventionResult(key=k, label=lbl, run_id=r.id, is_control=(k == "control"))
            for k, lbl, r in runs
        ],
    )
    store.put_experiment(experiment)
    return experiment.model_dump()


@app.get("/api/experiment/{experiment_id}")
def get_experiment(experiment_id: str) -> dict:
    """Poll an experiment; once every run lands, it carries its own verdict."""
    experiment = store.get_experiment(experiment_id)
    if not experiment:
        raise HTTPException(404, "Experiment not found")

    control = None
    for result in experiment.results:
        run = store.get_run(result.run_id)
        if not run:
            continue
        result.failed = run.status is RunStatus.FAILED
        if run.status is RunStatus.DONE:
            result.metrics = run.metrics
        if result.is_control:
            control = result

    if control and control.metrics.vehicles_loaded:
        for result in experiment.results:
            if result.is_control or result.failed:
                continue
            _, pct, _ = compare_metrics(control.metrics, result.metrics)
            result.deltas_pct = pct

    finished = all(
        (store.get_run(r.run_id) or SimulationRun(id="", network_id="", scenario=Scenario(id="")))
        .status in (RunStatus.DONE, RunStatus.FAILED)
        for r in experiment.results
    )
    if finished and control:
        diagnosis, recommendation, best = recommend_intervention(control, experiment.results)
        experiment.diagnosis = diagnosis
        experiment.recommendation = recommendation
        experiment.best_key = best

    payload = experiment.model_dump()
    payload["finished"] = finished
    return payload


def _busiest_segments(network: RoadNetwork, count: int) -> list[str]:
    """Highest-capacity corridor segments -- the ones worth intervening on."""
    ranked = sorted(
        network.segments,
        key=lambda s: (s.lanes, s.length_m, s.speed_limit_kmh),
        reverse=True,
    )
    return [s.id for s in ranked[:count]]


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict:
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    return _run_response(run)


@app.get("/api/runs/{run_id}/playback")
def get_playback(run_id: str) -> JSONResponse:
    path = store.playback_path(run_id)
    if not path.exists():
        raise HTTPException(404, "No playback for this run")
    return JSONResponse(json.loads(path.read_text(encoding="utf-8")))


@app.post("/api/compare")
def compare(request: CompareRequest) -> dict:
    baseline = store.get_run(request.baseline_run_id)
    scenario_run = store.get_run(request.scenario_run_id)
    if not baseline or not scenario_run:
        raise HTTPException(404, "Run not found")
    deltas, deltas_pct, verdict = compare_metrics(baseline.metrics, scenario_run.metrics)
    return Comparison(
        baseline_run_id=baseline.id,
        scenario_run_id=scenario_run.id,
        baseline=baseline.metrics,
        scenario=scenario_run.metrics,
        deltas=deltas,
        deltas_pct=deltas_pct,
        verdict=verdict,
    ).model_dump()


@app.get("/api/reality/scenes")
def reality_scenes() -> list[dict]:
    """Trained Gaussian splat scenes available for the reality view."""
    from ..config import DATA_DIR

    scenes = []
    for ply in sorted((DATA_DIR / "reality").glob("*/*.ply")):
        scenes.append({
            "id": ply.parent.name,
            "file": ply.name,
            "size_mb": round(ply.stat().st_size / 1e6, 1),
            "url": f"/api/reality/{ply.parent.name}/splat",
        })
    return scenes


@app.get("/api/reality/{scene_id}/splat")
def reality_splat(scene_id: str) -> FileResponse:
    """Serve the trained .ply. Large, so it is streamed rather than inlined."""
    from ..config import DATA_DIR

    # Resolve inside the reality dir and refuse anything that escapes it.
    base = (DATA_DIR / "reality").resolve()
    target = (base / scene_id).resolve()
    if not str(target).startswith(str(base)):
        raise HTTPException(400, "Invalid scene id")
    # Prefer the pruned scene: the raw training output carries ~20% oversized,
    # transparent and far-outlier Gaussians that render as white smears and haze.
    plys = sorted(target.glob("*_clean.ply")) or sorted(target.glob("*.ply"))
    if not plys:
        raise HTTPException(404, "No trained splat for this scene")
    return FileResponse(
        plys[0],
        media_type="application/octet-stream",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/api/reality/{scene_id}/cameras")
def reality_cameras(scene_id: str) -> JSONResponse:
    """Harvested GPS poses for the scene -- the correspondences Gate 4 needs."""
    from ..config import DATA_DIR

    base = (DATA_DIR / "reality").resolve()
    target = (base / scene_id).resolve()
    if not str(target).startswith(str(base)):
        raise HTTPException(400, "Invalid scene id")
    # Prefer the recovered COLMAP poses: they are in the same frame as the
    # splat, so the viewer can start at a real reconstructed viewpoint instead
    # of an arbitrary origin (which lands the camera inside the point cloud).
    cameras = target / "cameras.json"
    if cameras.exists():
        return JSONResponse(json.loads(cameras.read_text(encoding="utf-8")))
    poses = target / "poses.json"
    if not poses.exists():
        poses = base / "seq2016_dense" / "poses.json"
    if not poses.exists():
        raise HTTPException(404, "No poses for this scene")
    return JSONResponse(json.loads(poses.read_text(encoding="utf-8")))


@app.get("/api/strategies")
def strategies() -> list[dict]:
    return [
        # ACTUATED is deliberately not exposed: it requires the underlying TLS
        # programs to be built as actuated, and our OSM-derived plans are static,
        # so selecting it would silently reproduce the fixed-time result.
        {"key": SignalStrategy.FIXED.value, "label": "Fixed-time",
         "description": "Static plan synthesised from OSM signal tags"},
        {"key": SignalStrategy.ADAPTIVE.value, "label": "Adaptive",
         "description": "Queue-responsive green extension and early termination"},
        {"key": SignalStrategy.MAX_PRESSURE.value, "label": "Max-pressure",
         "description": "Decentralised throughput-maximising control with proven stability"},
    ]


# ------------------------------------------------------------------ helpers


def _network_response(network: RoadNetwork) -> dict:
    """Network metadata without the heavy geometry payload."""
    return {
        "id": network.id,
        "name": network.name,
        "bbox": network.bbox.model_dump(),
        "center": network.bbox.center.model_dump(),
        "stats": network.stats.model_dump(),
        "enrichment": network_summary(network),
        "signals": [s.model_dump() for s in network.signals],
        "segment_count": len(network.segments),
    }


def _run_response(run) -> dict:
    data = run.model_dump()
    data.pop("playback", None)
    return data
