"""The simulation engine: Scenario + RoadNetwork -> SimulationRun.

Performance architecture (this matters for a live demo):
  * SUMO's *native* outputs (tripinfo, summary, edgeData) produce all aggregate
    metrics. They are written by the C++ core at near-zero cost.
  * TraCI is used ONLY for things that genuinely require a control loop:
    dynamic scenario events, adaptive signal control, and playback capture.
  * Playback uses TraCI *subscriptions*, so capturing every vehicle costs one
    round-trip per step instead of three per vehicle per step.

Polling thousands of edges over TraCI each step would be ~100x slower and is
the usual reason naive SUMO integrations cannot be demoed in real time.
"""

from __future__ import annotations

import math
import shutil
import time
import uuid
from pathlib import Path
from xml.etree import ElementTree as ET

from ..config import RUNS_DIR, SUMO_BINARY
from ..contracts import (
    Metrics,
    PlaybackFrame,
    RoadNetwork,
    RunStatus,
    Scenario,
    SegmentMetric,
    SimulationRun,
    TimeseriesPoint,
    VehicleFrame,
)
from .analysis import find_bottlenecks
from .controllers import make_controller
from .demand import generate_routes
from .geo import GeoProjector
from .vtypes import build_vtype_xml

# Playback budget. Frames are the single biggest payload in the system, so we
# cap them deliberately rather than letting a long scenario blow up the client.
PLAYBACK_INTERVAL_S = 2.0
MAX_PLAYBACK_FRAMES = 250
MAX_VEHICLES_PER_FRAME = 1200

CLASS_SHORTHAND = {
    "car": "c", "motorcycle": "m", "auto_rickshaw": "a",
    "bus": "b", "truck": "t", "bicycle": "y",
}

# TraCI reports this sentinel (-2^30) whenever a value is unavailable, e.g. the
# position of a vehicle that is currently teleporting.
INVALID_SENTINEL = -1073741824.0


def _valid_position(position) -> bool:
    if not position or len(position) < 2:
        return False
    x, y = position[0], position[1]
    if x is None or y is None:
        return False
    if not (math.isfinite(x) and math.isfinite(y)):
        return False
    return x > INVALID_SENTINEL / 2 and y > INVALID_SENTINEL / 2


def _finite(value, default: float = 0.0) -> float:
    """Coerce a possibly-missing or sentinel TraCI scalar to a usable float."""
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number) or number <= INVALID_SENTINEL / 2:
        return default
    return number


def _write_sumocfg(run_dir: Path, net_path: Path, routes_path: Path, scenario: Scenario) -> Path:
    cfg = run_dir / "sim.sumocfg"
    # Sublane model is enabled by a non-zero lateral-resolution. Without it the
    # entire Indian-traffic calibration in vtypes.py has no effect.
    cfg.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<configuration>
  <input>
    <net-file value="{net_path}"/>
    <route-files value="{routes_path}"/>
    <additional-files value="vtypes.add.xml,outputs.add.xml"/>
  </input>
  <time>
    <begin value="0"/>
    <end value="{scenario.duration_s}"/>
    <step-length value="1.0"/>
  </time>
  <processing>
    <lateral-resolution value="{scenario.lateral_resolution_m}"/>
    <collision.action value="warn"/>
    <collision.mingap-factor value="0"/>
    <!-- Closing a lane can sever the only connection to a downstream edge,
         which SUMO otherwise treats as a FATAL route error and quits on.
         Any dynamic closure needs this or the scenario cannot be simulated. -->
    <ignore-route-errors value="true"/>
    <time-to-teleport value="180"/>
    <time-to-teleport.disconnected value="10"/>
    <max-depart-delay value="900"/>
    <ignore-junction-blocker value="20"/>
    <default.speeddev value="0.12"/>
  </processing>
  <routing>
    <!-- Only a minority of drivers have live routing and will divert around a
         closure. At 0.85 the fleet reroutes so perfectly that lane closures
         produce almost no measurable impact, which is not what happens. -->
    <device.rerouting.probability value="0.30"/>
    <device.rerouting.period value="60"/>
    <device.rerouting.adaptation-steps value="18"/>
    <device.emissions.probability value="1.0"/>
  </routing>
  <output>
    <tripinfo-output value="tripinfo.xml"/>
    <summary-output value="summary.xml"/>
  </output>
  <report>
    <no-warnings value="true"/>
    <no-step-log value="true"/>
    <duration-log.statistics value="true"/>
  </report>
  <random_number>
    <seed value="{scenario.seed}"/>
  </random_number>
</configuration>
""",
        encoding="utf-8",
    )
    return cfg


def _write_outputs_add(run_dir: Path, duration_s: int) -> None:
    """One edgeData aggregation over the whole run -> per-segment metrics."""
    (run_dir / "outputs.add.xml").write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<additional>
  <edgeData id="edges" file="edgedata.xml" begin="0" end="{duration_s}"
            period="{duration_s}" excludeEmpty="false" withInternal="false"/>
</additional>
""",
        encoding="utf-8",
    )


# ------------------------------------------------------------------ scenario


def _apply_static_scenario(conn, scenario: Scenario, network: RoadNetwork) -> None:
    """Apply everything that is true from t=0: obstructions and signal setup."""
    segment_ids = {s.id for s in network.segments}

    for obstruction in scenario.obstructions:
        if obstruction.segment_id not in segment_ids:
            continue
        try:
            lane_count = conn.edge.getLaneNumber(obstruction.segment_id)
        except Exception:  # noqa: BLE001
            continue
        # A pothole/vendor/parked vehicle does not close a lane; it degrades the
        # usable speed and effective width around it. Severity scales that.
        factor = max(0.15, 1.0 - 0.8 * obstruction.severity)
        for i in range(lane_count):
            lane_id = f"{obstruction.segment_id}_{i}"
            try:
                current = conn.lane.getMaxSpeed(lane_id)
                conn.lane.setMaxSpeed(lane_id, max(1.5, current * factor))
            except Exception:  # noqa: BLE001
                continue


def _close_lanes(conn, closure, closed_state: dict) -> None:
    """Disallow all vehicle classes on the closed lanes of a segment."""
    try:
        lane_count = conn.edge.getLaneNumber(closure.segment_id)
    except Exception:  # noqa: BLE001
        return
    # Close from the leftmost lane inward; lane 0 is kerbside in SUMO.
    to_close = min(closure.lanes_closed, max(0, lane_count - 1))
    for offset in range(to_close):
        lane_id = f"{closure.segment_id}_{lane_count - 1 - offset}"
        if lane_id in closed_state:
            continue
        try:
            closed_state[lane_id] = conn.lane.getAllowed(lane_id)
            conn.lane.setAllowed(lane_id, [])
        except Exception:  # noqa: BLE001
            continue

    # Drivers already committed to this segment reconsider when they meet the
    # closure. Without this they queue into it regardless, which overstates the
    # impact; with it we get realistic diversion onto parallel streets.
    _reroute_through(conn, closure.segment_id)


def _reroute_through(conn, segment_id: str) -> None:
    """Re-route every vehicle whose remaining path uses `segment_id`."""
    try:
        vehicle_ids = conn.vehicle.getIDList()
    except Exception:  # noqa: BLE001
        return
    for veh_id in vehicle_ids:
        try:
            route = conn.vehicle.getRoute(veh_id)
            index = conn.vehicle.getRouteIndex(veh_id)
            if segment_id in route[max(0, index):]:
                conn.vehicle.rerouteTraveltime(veh_id, currentTravelTimes=True)
        except Exception:  # noqa: BLE001
            continue


def _reopen_lanes(conn, closure, closed_state: dict) -> None:
    try:
        lane_count = conn.edge.getLaneNumber(closure.segment_id)
    except Exception:  # noqa: BLE001
        return
    to_close = min(closure.lanes_closed, max(0, lane_count - 1))
    for offset in range(to_close):
        lane_id = f"{closure.segment_id}_{lane_count - 1 - offset}"
        if lane_id not in closed_state:
            continue
        try:
            conn.lane.setAllowed(lane_id, closed_state.pop(lane_id))
        except Exception:  # noqa: BLE001
            continue


def _spawn_incident(conn, incident, index: int) -> str | None:
    """Park a broken-down vehicle mid-segment for a fixed duration."""
    veh_id = f"incident_{index}"
    route_id = f"incident_route_{index}"
    try:
        conn.route.add(route_id, [incident.segment_id])
        conn.vehicle.add(veh_id, route_id, typeID="car", departLane="best", departSpeed="0")
        length = conn.lane.getLength(f"{incident.segment_id}_0")
        pos = max(5.0, min(length - 5.0, length * incident.position))
        conn.vehicle.setStop(
            veh_id,
            incident.segment_id,
            pos=pos,
            laneIndex=incident.lane_index,
            duration=incident.duration_s,
        )
        conn.vehicle.setColor(veh_id, (255, 40, 40, 255))
        return veh_id
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------ parsing


def _parse_tripinfo(path: Path, duration_s: int) -> tuple[Metrics, dict]:
    """Aggregate per-vehicle trip records into headline metrics."""
    metrics = Metrics()
    if not path.exists():
        return metrics, {}

    count = 0
    sum_duration = sum_timeloss = sum_waiting = sum_route = sum_speed = 0.0
    sum_co2 = sum_fuel = 0.0
    class_counts: dict[str, int] = {}

    for _event, elem in ET.iterparse(str(path), events=("end",)):
        if elem.tag == "tripinfo":
            count += 1
            duration = float(elem.get("duration", 0) or 0)
            route_length = float(elem.get("routeLength", 0) or 0)
            sum_duration += duration
            sum_timeloss += float(elem.get("timeLoss", 0) or 0)
            sum_waiting += float(elem.get("waitingTime", 0) or 0)
            sum_route += route_length
            if duration > 0:
                sum_speed += (route_length / duration) * 3.6
            vtype = (elem.get("vType") or "").split("@")[0]
            class_counts[vtype] = class_counts.get(vtype, 0) + 1
            elem.clear()
        elif elem.tag == "emissions":
            sum_co2 += float(elem.get("CO2_abs", 0) or 0)
            sum_fuel += float(elem.get("fuel_abs", 0) or 0)
            elem.clear()

    if count:
        metrics.vehicles_arrived = count
        metrics.avg_travel_time_s = round(sum_duration / count, 1)
        metrics.avg_delay_s = round(sum_timeloss / count, 1)
        metrics.avg_speed_kmh = round(sum_speed / count, 1)
        # Fraction of journey time that was lost to congestion: a bounded,
        # interpretable congestion index that does not depend on network size.
        metrics.congestion_index = round(
            min(1.0, sum_timeloss / max(sum_duration, 1.0)), 3
        )
    hours = max(duration_s / 3600.0, 1e-6)
    metrics.throughput_veh_hr = round(count / hours, 0)
    metrics.total_co2_kg = round(sum_co2 / 1e6, 2)   # mg -> kg
    metrics.total_fuel_l = round(sum_fuel / 1e6, 2)  # mg -> l (approx, SUMO ml*1e3)
    return metrics, class_counts


def _parse_summary(path: Path) -> tuple[list[TimeseriesPoint], int, int]:
    """Per-step network aggregates -> timeseries, loaded count, teleports."""
    points: list[TimeseriesPoint] = []
    loaded = teleports = 0
    if not path.exists():
        return points, loaded, teleports

    for _event, elem in ET.iterparse(str(path), events=("end",)):
        if elem.tag != "step":
            continue
        t = float(elem.get("time", 0) or 0)
        loaded = max(loaded, int(float(elem.get("loaded", 0) or 0)))
        teleports = max(teleports, int(float(elem.get("teleports", 0) or 0)))
        # Sample the timeseries every 5s; per-second resolution is noise on a chart.
        if t % 5 == 0:
            points.append(
                TimeseriesPoint(
                    t=t,
                    mean_speed_kmh=round(float(elem.get("meanSpeed", 0) or 0) * 3.6, 2),
                    running=int(float(elem.get("running", 0) or 0)),
                    arrived_cumulative=int(float(elem.get("ended", 0) or 0)),
                    halting=int(float(elem.get("halting", 0) or 0)),
                    mean_waiting_s=round(float(elem.get("meanWaitingTime", 0) or 0), 2),
                )
            )
        elem.clear()
    return points, loaded, teleports


def _parse_edgedata(path: Path, network: RoadNetwork) -> list[SegmentMetric]:
    """Per-edge aggregates -> the congestion heatmap source."""
    if not path.exists():
        return []
    free_flow = {s.id: s.speed_limit_kmh for s in network.segments}
    lengths = {s.id: s.length_m for s in network.segments}
    out: list[SegmentMetric] = []

    for _event, elem in ET.iterparse(str(path), events=("end",)):
        if elem.tag != "edge":
            continue
        eid = elem.get("id", "")
        if eid.startswith(":") or eid not in free_flow:
            elem.clear()
            continue
        speed_ms = float(elem.get("speed", 0) or 0)
        speed_kmh = speed_ms * 3.6
        ff = free_flow.get(eid, 50.0) or 50.0
        density = float(elem.get("density", 0) or 0)
        occupancy = float(elem.get("occupancy", 0) or 0)
        waiting = float(elem.get("waitingTime", 0) or 0)
        entered = float(elem.get("entered", 0) or 0)
        # Queue proxy: occupied fraction of the segment. Robust and cheap.
        queue_m = round(lengths.get(eid, 0.0) * min(1.0, occupancy / 100.0), 1)

        out.append(
            SegmentMetric(
                segment_id=eid,
                mean_speed_kmh=round(speed_kmh, 2),
                free_flow_speed_kmh=round(ff, 1),
                speed_ratio=round(min(1.0, speed_kmh / ff), 3) if ff else 1.0,
                density_veh_km=round(density, 2),
                max_queue_m=queue_m,
                mean_waiting_s=round(waiting, 2),
                vehicles_seen=int(entered),
            )
        )
        elem.clear()
    return out


# ------------------------------------------------------------------ main


def run_simulation(
    network: RoadNetwork,
    scenario: Scenario,
    run_id: str | None = None,
    progress_cb=None,
    capture_playback: bool = True,
) -> SimulationRun:
    """Execute one scenario. Self-contained and process-safe."""
    run_id = run_id or f"run_{uuid.uuid4().hex[:10]}"
    run = SimulationRun(
        id=run_id, network_id=network.id, scenario=scenario, status=RunStatus.BUILDING
    )
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()

    try:
        net_path = Path(network.sumo_net_path)
        if not net_path.exists():
            raise RuntimeError(f"SUMO network missing at {net_path}")

        routes_path = generate_routes(
            net_path,
            lane_km=network.stats.lane_km,
            duration_s=scenario.duration_s,
            demand_multiplier=scenario.demand_multiplier,
            seed=scenario.seed,
        )

        (run_dir / "vtypes.add.xml").write_text(
            build_vtype_xml(
                scenario.vehicle_mix, scenario.lane_discipline, scenario.weather.value
            ),
            encoding="utf-8",
        )
        _write_outputs_add(run_dir, scenario.duration_s)
        cfg = _write_sumocfg(run_dir, net_path.resolve(), routes_path.resolve(), scenario)

        import traci
        import traci.constants as tc

        # SUMO resolves relative paths inside a config against the config's own
        # directory, so an absolute cfg path puts every output into run_dir.
        label = f"rt_{run_id}"
        traci.start(
            [SUMO_BINARY, "-c", str(cfg.resolve()), "--no-warnings", "true"],
            label=label,
        )
        conn = traci.getConnection(label)
        run.status = RunStatus.RUNNING

        projector = GeoProjector(net_path)
        tls_ids = list(conn.trafficlight.getIDList())
        controller = make_controller(scenario.signal_strategy, conn, tls_ids)

        _apply_static_scenario(conn, scenario, network)

        # Schedule dynamic events
        closures = sorted(scenario.lane_closures, key=lambda c: c.start_s)
        incidents = sorted(scenario.incidents, key=lambda i: i.start_s)
        closed_state: dict[str, list] = {}
        active_closures: list = []
        pending_incidents = list(enumerate(incidents))

        conn.simulation.subscribe([tc.VAR_DEPARTED_VEHICLES_IDS])

        frames: list[PlaybackFrame] = []
        next_frame_t = 0.0
        frame_stride = max(
            PLAYBACK_INTERVAL_S,
            scenario.duration_s / MAX_PLAYBACK_FRAMES,
        )

        t = 0.0
        end_t = float(scenario.duration_s)
        while t < end_t:
            # --- dynamic scenario events ---
            for closure in closures:
                if closure.start_s <= t < closure.end_s and closure not in active_closures:
                    _close_lanes(conn, closure, closed_state)
                    active_closures.append(closure)
                elif t >= closure.end_s and closure in active_closures:
                    _reopen_lanes(conn, closure, closed_state)
                    active_closures.remove(closure)

            still_pending = []
            for index, incident in pending_incidents:
                if t >= incident.start_s:
                    _spawn_incident(conn, incident, index)
                else:
                    still_pending.append((index, incident))
            pending_incidents = still_pending

            controller.step(t)

            conn.simulationStep()
            t = conn.simulation.getTime()

            # --- playback capture via subscriptions ---
            if capture_playback:
                sim_sub = conn.simulation.getSubscriptionResults() or {}
                for veh_id in sim_sub.get(tc.VAR_DEPARTED_VEHICLES_IDS, ()):
                    conn.vehicle.subscribe(
                        veh_id,
                        [tc.VAR_POSITION, tc.VAR_ANGLE, tc.VAR_SPEED, tc.VAR_TYPE],
                    )
                if t >= next_frame_t and len(frames) < MAX_PLAYBACK_FRAMES:
                    next_frame_t = t + frame_stride
                    results = conn.vehicle.getAllSubscriptionResults() or {}
                    # A vehicle that is mid-teleport or otherwise off-network
                    # reports SUMO's sentinel position (-2^30). Projecting that
                    # yields infinity, which pydantic serialises as JSON null --
                    # producing a document the parent process cannot re-validate.
                    # Drop those vehicles: they have no location to draw.
                    items = [
                        (veh_id, res)
                        for veh_id, res in list(results.items())[:MAX_VEHICLES_PER_FRAME]
                        if _valid_position(res.get(tc.VAR_POSITION))
                    ]
                    coords = [r[tc.VAR_POSITION] for _v, r in items]
                    lonlats = projector.many(coords)
                    vehicles = []
                    for (veh_id, res), (lon, lat) in zip(items, lonlats):
                        if not (math.isfinite(lon) and math.isfinite(lat)):
                            continue
                        vtype = (res.get(tc.VAR_TYPE) or "car").split("@")[0]
                        vehicles.append(
                            VehicleFrame(
                                id=veh_id,
                                c=CLASS_SHORTHAND.get(vtype, "c"),
                                # 5 dp is ~1.1 m -- below the size of a vehicle
                                # dot, and a third smaller on the wire than 6 dp.
                                lon=round(lon, 5),
                                lat=round(lat, 5),
                                a=round(_finite(res.get(tc.VAR_ANGLE)), 1),
                                s=round(_finite(res.get(tc.VAR_SPEED)) * 3.6, 1),
                            )
                        )
                    frames.append(PlaybackFrame(t=t, vehicles=vehicles))

            if progress_cb and int(t) % 30 == 0:
                progress_cb(min(0.99, t / end_t))

            # Stop early once the network has drained.
            if conn.simulation.getMinExpectedNumber() <= 0:
                break

        conn.close()

        # ---------------- results ----------------
        metrics, class_counts = _parse_tripinfo(run_dir / "tripinfo.xml", scenario.duration_s)
        timeseries, loaded, teleports = _parse_summary(run_dir / "summary.xml")
        segment_metrics = _parse_edgedata(run_dir / "edgedata.xml", network)

        metrics.vehicles_loaded = loaded
        metrics.teleports = teleports
        metrics.vehicles_still_running = max(0, loaded - metrics.vehicles_arrived)
        if segment_metrics:
            metrics.max_queue_m = round(max(s.max_queue_m for s in segment_metrics), 1)
            metrics.total_queue_m = round(sum(s.max_queue_m for s in segment_metrics), 1)

        run.metrics = metrics
        run.timeseries = timeseries
        run.segment_metrics = segment_metrics
        run.bottlenecks = find_bottlenecks(network, segment_metrics, scenario)
        run.playback = frames
        run.sim_seconds = t
        run.wall_seconds = round(time.perf_counter() - started, 2)
        run.realtime_factor = round(t / max(run.wall_seconds, 1e-6), 1)
        run.progress = 1.0
        run.status = RunStatus.DONE

    except Exception as exc:  # noqa: BLE001 - a failed run is a result, not a crash
        run.status = RunStatus.FAILED
        run.error = f"{type(exc).__name__}: {exc}"
        run.wall_seconds = round(time.perf_counter() - started, 2)
        try:
            import traci

            traci.close()
        except Exception:  # noqa: BLE001
            pass

    return run


def cleanup_run(run_id: str) -> None:
    shutil.rmtree(RUNS_DIR / run_id, ignore_errors=True)
