"""OSM -> SUMO network -> canonical RoadNetwork.

This is the "high-fidelity road network modeling" core of the project.

We deliberately delegate the hard geometry to `netconvert`: junction joining,
turn-lane inference, connection topology, signal program synthesis and
right-of-way computation are decades of work we are not going to reimplement
in a hackathon. Our contribution is (a) driving it with parameters that are
correct for Indian roads, and (b) lifting the result into a clean, typed,
simulation-agnostic RoadNetwork that the rest of the system consumes.
"""

from __future__ import annotations

import hashlib
import subprocess
import time
from pathlib import Path

from ..config import CACHE_DIR, NETCONVERT
from ..contracts import (
    BBox,
    Junction,
    JunctionType,
    NetworkStats,
    RoadClass,
    RoadNetwork,
    RoadSegment,
    TrafficSignal,
)
from .fetch import cache_key, fetch_osm

# OSM highway tag -> our simulation-relevant class
ROAD_CLASS_MAP: dict[str, RoadClass] = {
    "motorway": RoadClass.MOTORWAY,
    "trunk": RoadClass.TRUNK,
    "primary": RoadClass.ARTERIAL,
    "secondary": RoadClass.SUB_ARTERIAL,
    "tertiary": RoadClass.COLLECTOR,
    "residential": RoadClass.LOCAL,
    "unclassified": RoadClass.LOCAL,
    "living_street": RoadClass.LOCAL,
    "service": RoadClass.SERVICE,
    "road": RoadClass.OTHER,
}

SUMO_NODE_TYPE_MAP: dict[str, JunctionType] = {
    "traffic_light": JunctionType.SIGNALISED,
    "traffic_light_unregulated": JunctionType.SIGNALISED,
    "traffic_light_right_on_red": JunctionType.SIGNALISED,
    "priority": JunctionType.PRIORITY,
    "priority_stop": JunctionType.PRIORITY,
    "right_before_left": JunctionType.UNCONTROLLED,
    "left_before_right": JunctionType.UNCONTROLLED,
    "allway_stop": JunctionType.UNCONTROLLED,
    "dead_end": JunctionType.DEAD_END,
    "internal": JunctionType.INTERNAL,
    "zipper": JunctionType.PRIORITY,
    "unregulated": JunctionType.UNCONTROLLED,
}


def _classify(sumo_type: str) -> RoadClass:
    """SUMO edge types look like 'highway.primary' or 'highway.primary_link'."""
    raw = (sumo_type or "").split(".")[-1]
    if raw.endswith("_link"):
        return RoadClass.LINK
    return ROAD_CLASS_MAP.get(raw, RoadClass.OTHER)


def _netconvert_args(osm_path: Path, net_path: Path) -> list[str]:
    """netconvert invocation tuned for Indian urban road networks.

    Every flag here is a modelling decision, not boilerplate:
      --lefthand          India drives on the left. Without this every junction's
                          turn geometry and right-of-way is mirrored and wrong.
      --junctions.join    OSM splits a real intersection into 3-6 nodes. Joining
                          them is what turns a map into a simulatable junction.
      --tls.guess-signals promotes signalised OSM nodes into real TLS programs.
      --osm.turn-lanes    reads turn:lanes tags -> proper lane-level connections.
      no --no-turnarounds U-turns are a defining feature of Indian junctions;
                          we explicitly keep them enabled.
    """
    return [
        NETCONVERT,
        "--osm-files", str(osm_path),
        "--output-file", str(net_path),
        # --- left-hand traffic ---
        "--lefthand",
        # --- projection: metric, so lengths and speeds are physical ---
        "--proj.utm",
        # --- junction reconstruction ---
        "--junctions.join",
        "--junctions.join-dist", "18",
        "--junctions.corner-detail", "5",
        "--junctions.limit-turn-speed", "5.5",
        # --- signals ---
        "--tls.guess-signals",
        "--tls.join",
        "--tls.discard-simple",
        "--tls.default-type", "static",
        "--tls.cycle.time", "90",
        # --- geometry & topology cleanup ---
        "--geometry.remove",
        "--geometry.max-grade.fix",
        "--roundabouts.guess",
        "--ramps.guess",
        "--edges.join",
        "--remove-edges.isolated",
        "--keep-edges.components", "1",
        # --- OSM detail we want to preserve ---
        "--osm.turn-lanes",
        "--osm.elevation", "false",
        "--output.street-names", "true",
        "--output.original-names", "true",
        # --- defaults for under-tagged Indian roads ---
        "--default.lanenumber", "2",
        "--default.speed", "13.89",  # 50 km/h
        "--default.junctions.keep-clear", "false",  # Indian junctions are not kept clear
        # --- noise control ---
        "--no-warnings",
        "--verbose", "false",
    ]


def build_sumo_net(osm_path: Path, net_path: Path) -> None:
    """Run netconvert. Raises RuntimeError with stderr on failure."""
    result = subprocess.run(
        _netconvert_args(osm_path, net_path),
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0 or not net_path.exists():
        raise RuntimeError(
            f"netconvert failed (exit {result.returncode}).\n"
            f"stderr:\n{result.stderr[-3000:]}"
        )


def net_to_road_network(
    net_path: Path, network_id: str, name: str, bbox: BBox, build_seconds: float
) -> RoadNetwork:
    """Lift a SUMO .net.xml into our canonical, simulator-agnostic RoadNetwork."""
    import sumolib  # imported here so config.py has already set SUMO_HOME

    # withPrograms=True is required or getPrograms() silently returns {} and we
    # lose every signal phase plan.
    net = sumolib.net.readNet(str(net_path), withPrograms=True, withInternal=False)

    def to_lonlat(x: float, y: float) -> tuple[float, float]:
        lon, lat = net.convertXY2LonLat(x, y)
        return lon, lat

    # netconvert names guessed signals "GS_<nodeid>". TraCI addresses the signal
    # by the TLS id, while geometry lives on the node -- so map between them once.
    tls_by_node: dict[str, str] = {}
    for tls in net.getTrafficLights():
        tls_id = tls.getID()
        node_id = tls_id[3:] if tls_id.startswith("GS_") else tls_id
        tls_by_node[node_id] = tls_id

    # ---------------- junctions ----------------
    junctions: list[Junction] = []
    roundabout_nodes: set[str] = set()
    for ra in net.getRoundabouts():
        roundabout_nodes.update(ra.getNodes())

    for node in net.getNodes():
        nid = node.getID()
        x, y = node.getCoord()
        lon, lat = to_lonlat(x, y)
        jtype = SUMO_NODE_TYPE_MAP.get(node.getType(), JunctionType.PRIORITY)
        if nid in roundabout_nodes and jtype is not JunctionType.SIGNALISED:
            jtype = JunctionType.ROUNDABOUT

        incoming = [e.getID() for e in node.getIncoming() if not e.getID().startswith(":")]
        outgoing = [e.getID() for e in node.getOutgoing() if not e.getID().startswith(":")]
        junctions.append(
            Junction(
                id=nid,
                lat=lat,
                lon=lon,
                type=jtype,
                incoming_edges=incoming,
                outgoing_edges=outgoing,
                degree=len(set(incoming) | set(outgoing)),
                signal_id=tls_by_node.get(nid),
            )
        )

    # ---------------- segments ----------------
    segments: list[RoadSegment] = []
    total_length = 0.0
    lane_km = 0.0
    for edge in net.getEdges():
        eid = edge.getID()
        if eid.startswith(":"):  # internal junction edge
            continue
        length = edge.getLength()
        lanes = edge.getLaneNumber()
        speed_kmh = edge.getSpeed() * 3.6
        geometry = [list(to_lonlat(x, y)) for x, y in edge.getShape()]

        total_length += length
        lane_km += length * lanes

        # An edge in SUMO is already directional. A road is one-way if SUMO
        # produced no opposite-direction twin between the same node pair.
        from_id = edge.getFromNode().getID()
        to_id = edge.getToNode().getID()
        has_reverse = any(
            e.getToNode().getID() == from_id
            for e in edge.getToNode().getOutgoing()
            if not e.getID().startswith(":")
        )

        segments.append(
            RoadSegment(
                id=eid,
                from_junction=from_id,
                to_junction=to_id,
                name=edge.getName() or "",
                road_class=_classify(edge.getType()),
                length_m=round(length, 2),
                lanes=lanes,
                speed_limit_kmh=round(speed_kmh, 1),
                oneway=not has_reverse,
                geometry=geometry,
            )
        )

    # ---------------- signals ----------------
    signals: list[TrafficSignal] = []
    junction_by_id = {j.id: j for j in junctions}
    for tls in net.getTrafficLights():
        tid = tls.getID()
        node_id = tid[3:] if tid.startswith("GS_") else tid
        node = junction_by_id.get(node_id)
        phases: list[dict] = []
        cycle = 0.0
        programs = tls.getPrograms()
        if programs:
            program = next(iter(programs.values()))
            for phase in program.getPhases():
                phases.append({"state": phase.state, "duration": phase.duration})
                cycle += float(phase.duration)
        # getConnections() yields (inLane, outLane, linkIndex) triples
        controlled = sorted(
            {conn[0].getID() for conn in tls.getConnections() if conn and conn[0]}
        )
        signals.append(
            TrafficSignal(
                id=tid,
                junction_id=node_id,
                lat=node.lat if node else 0.0,
                lon=node.lon if node else 0.0,
                phases=phases,
                cycle_time_s=cycle,
                controlled_lanes=controlled[:64],
            )
        )

    signalised = sum(1 for j in junctions if j.type is JunctionType.SIGNALISED)
    roundabouts = len(net.getRoundabouts())

    # Acceleration claim, derived rather than asserted. Calibrated against
    # published manual-modelling effort for microsimulation networks:
    # roughly 0.5 h per junction plus 0.2 h per km of coded link geometry.
    manual_hours = 0.5 * len(junctions) + 0.2 * (total_length / 1000.0)

    stats = NetworkStats(
        junctions=len(junctions),
        signalised_junctions=signalised,
        segments=len(segments),
        lane_km=round(lane_km / 1000.0, 3),
        total_length_km=round(total_length / 1000.0, 3),
        roundabouts=roundabouts,
        build_seconds=round(build_seconds, 2),
        manual_effort_hours_estimate=round(manual_hours, 1),
    )

    return RoadNetwork(
        id=network_id,
        name=name,
        bbox=bbox,
        junctions=junctions,
        segments=segments,
        signals=signals,
        stats=stats,
        sumo_net_path=str(net_path),
    )


def build_network(
    bbox: BBox, name: str, force: bool = False, network_id: str | None = None
) -> RoadNetwork:
    """Full pipeline: Overpass -> netconvert -> RoadNetwork. The headline function."""
    started = time.perf_counter()
    key = cache_key(bbox)
    network_id = network_id or f"net_{key}"

    osm_path = fetch_osm(bbox, force=force)
    net_path = CACHE_DIR / f"{network_id}.net.xml"
    if force or not net_path.exists():
        build_sumo_net(osm_path, net_path)

    elapsed = time.perf_counter() - started
    return net_to_road_network(net_path, network_id, name, bbox, elapsed)
