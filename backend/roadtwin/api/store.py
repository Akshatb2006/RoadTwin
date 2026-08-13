"""Network/run persistence and the parallel simulation scheduler.

Simulations are executed in a ProcessPoolExecutor rather than threads for a
concrete reason: TraCI keeps per-process global connection state, so two
simulations in one process would fight over it. Separate processes also let a
scenario sweep genuinely use every core -- which is what turns "compare 6
signal plans" from a six-minute wait into a one-minute one.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from ..config import CACHE_DIR
from ..contracts import RoadNetwork, RunStatus, Scenario, SimulationRun

MAX_WORKERS = max(2, min(8, (os.cpu_count() or 4) - 1))

_executor: ProcessPoolExecutor | None = None
_lock = threading.Lock()

# In-memory indexes. A hackathon does not need Postgres for this, and a restart
# losing run history is an acceptable trade for zero setup cost.
_networks: dict[str, RoadNetwork] = {}
_runs: dict[str, SimulationRun] = {}


def get_executor() -> ProcessPoolExecutor:
    global _executor
    with _lock:
        if _executor is None:
            _executor = ProcessPoolExecutor(max_workers=MAX_WORKERS)
        return _executor


# ------------------------------------------------------------------ networks


def network_path(network_id: str) -> Path:
    return CACHE_DIR / f"{network_id}.network.json"


def save_network(network: RoadNetwork) -> None:
    _networks[network.id] = network
    network_path(network.id).write_text(network.model_dump_json(), encoding="utf-8")


def load_network(network_id: str) -> RoadNetwork | None:
    if network_id in _networks:
        return _networks[network_id]
    path = network_path(network_id)
    if not path.exists():
        return None
    network = RoadNetwork.model_validate_json(path.read_text(encoding="utf-8"))
    _networks[network_id] = network
    return network


def list_networks() -> list[RoadNetwork]:
    for path in CACHE_DIR.glob("*.network.json"):
        network_id = path.name.replace(".network.json", "")
        if network_id not in _networks:
            load_network(network_id)
    return list(_networks.values())


# ---------------------------------------------------------------------- runs


def new_run_id() -> str:
    return f"run_{uuid.uuid4().hex[:10]}"


def put_run(run: SimulationRun) -> None:
    _runs[run.id] = run


def get_run(run_id: str) -> SimulationRun | None:
    return _runs.get(run_id)


def list_runs(limit: int = 50) -> list[SimulationRun]:
    return list(_runs.values())[-limit:]


def playback_path(run_id: str) -> Path:
    return CACHE_DIR / f"{run_id}.playback.json"


def save_playback(run: SimulationRun) -> None:
    """Playback is the largest payload by far, so it is stored and served
    separately instead of being inlined into every run response."""
    frames = [
        {
            "t": frame.t,
            "v": [
                [v.lon, v.lat, v.a, v.s, v.c] for v in frame.vehicles
            ],
        }
        for frame in run.playback
    ]
    playback_path(run.id).write_text(json.dumps({"frames": frames}), encoding="utf-8")
    run.playback = []  # keep it out of the run document


# ------------------------------------------------------------------- worker


def _worker(network_id: str, scenario_json: str, run_id: str, capture_playback: bool) -> str:
    """Runs inside a pool process. Returns the serialised SimulationRun."""
    from ..sim.runner import run_simulation  # imported here to keep the parent light

    network = load_network(network_id)
    if network is None:
        raise RuntimeError(f"Unknown network {network_id}")
    scenario = Scenario.model_validate_json(scenario_json)
    run = run_simulation(network, scenario, run_id=run_id, capture_playback=capture_playback)
    # Write playback to disk here, inside the worker, and strip it from the run.
    # A 15-minute scenario carries ~400k vehicle samples (~29 MB of JSON);
    # pushing that back through the process boundary just to write it to disk
    # in the parent dominates the cost of the simulation itself.
    save_playback(run)
    return run.model_dump_json()


def submit_run(
    network: RoadNetwork, scenario: Scenario, capture_playback: bool = True
) -> SimulationRun:
    """Queue a simulation and return immediately with a QUEUED run."""
    run_id = new_run_id()
    run = SimulationRun(
        id=run_id, network_id=network.id, scenario=scenario, status=RunStatus.QUEUED
    )
    put_run(run)

    future = get_executor().submit(
        _worker, network.id, scenario.model_dump_json(), run_id, capture_playback
    )

    def _done(fut) -> None:
        try:
            # Playback was already persisted by the worker and stripped.
            finished = SimulationRun.model_validate_json(fut.result())
            put_run(finished)
        except Exception as exc:  # noqa: BLE001
            failed = get_run(run_id)
            if failed:
                failed.status = RunStatus.FAILED
                failed.error = f"{type(exc).__name__}: {exc}"
                put_run(failed)

    future.add_done_callback(_done)
    run.status = RunStatus.RUNNING
    put_run(run)
    return run
