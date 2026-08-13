"""RoadTwin HTTP API."""

from __future__ import annotations

import json
import time
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..ai.planner import parse_scenario
from ..config import PRESETS
from ..contracts import BBox, Comparison, RoadNetwork, Scenario, SignalStrategy
from ..enrich.indian import enrich_network, network_summary
from ..osm.build import build_network
from ..sim.analysis import compare_metrics
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
