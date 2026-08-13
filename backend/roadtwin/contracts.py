"""
RoadTwin canonical contracts.

THESE ARE FROZEN. Every engine in the system talks through these three types:

    RoadNetwork   -- what the world looks like        (OSM/GIS engines produce it)
    Scenario      -- what we want to ask of the world (UI / AI planner produce it)
    SimulationRun -- what happened                    (SUMO engine produces it)

Nothing else crosses a module boundary. If you need to add a field, add it here
first, then update producers and consumers. Do not invent parallel dicts.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0.0"


# --------------------------------------------------------------------------
# 1. ROAD NETWORK
# --------------------------------------------------------------------------


class RoadClass(str, Enum):
    """Simulation-relevant road classes, normalised from OSM highway tags."""

    MOTORWAY = "motorway"
    TRUNK = "trunk"
    ARTERIAL = "arterial"       # primary
    SUB_ARTERIAL = "sub_arterial"  # secondary
    COLLECTOR = "collector"     # tertiary
    LOCAL = "local"             # residential / unclassified
    SERVICE = "service"
    LINK = "link"               # on/off ramps
    OTHER = "other"


class JunctionType(str, Enum):
    SIGNALISED = "signalised"
    PRIORITY = "priority"
    ROUNDABOUT = "roundabout"
    UNCONTROLLED = "uncontrolled"
    DEAD_END = "dead_end"
    INTERNAL = "internal"


class SurfaceQuality(str, Enum):
    GOOD = "good"
    FAIR = "fair"
    POOR = "poor"
    UNKNOWN = "unknown"


class LatLon(BaseModel):
    lat: float
    lon: float


class BBox(BaseModel):
    """Geographic bounding box. south/west/north/east in WGS84 degrees."""

    south: float
    west: float
    north: float
    east: float

    def as_overpass(self) -> str:
        return f"{self.south},{self.west},{self.north},{self.east}"

    @property
    def center(self) -> LatLon:
        return LatLon(lat=(self.south + self.north) / 2, lon=(self.west + self.east) / 2)


class Junction(BaseModel):
    """A node in the road graph. Maps 1:1 to a SUMO junction."""

    id: str
    lat: float
    lon: float
    type: JunctionType
    incoming_edges: list[str] = Field(default_factory=list)
    outgoing_edges: list[str] = Field(default_factory=list)
    # degree = number of distinct road approaches; drives complexity scoring
    degree: int = 0
    # populated only for signalised junctions
    signal_id: Optional[str] = None


class RoadSegment(BaseModel):
    """A directed edge in the road graph. Maps 1:1 to a SUMO edge."""

    id: str
    from_junction: str
    to_junction: str
    name: str = ""
    road_class: RoadClass = RoadClass.OTHER
    length_m: float
    lanes: int = 1
    speed_limit_kmh: float = 50.0
    oneway: bool = False
    # [[lon, lat], ...] polyline for rendering, in WGS84
    geometry: list[list[float]] = Field(default_factory=list)
    # ---- Indian-context enrichment (filled by enrich/indian.py) ----
    effective_lanes: float = 0.0     # capacity after encroachment / parking
    surface: SurfaceQuality = SurfaceQuality.UNKNOWN
    encroachment: float = 0.0        # 0..1 fraction of width lost
    two_wheeler_share: float = 0.0   # expected share of motorised 2W
    capacity_pcu_hr: float = 0.0     # PCU/hr, derived not guessed
    notes: list[str] = Field(default_factory=list)


class TrafficSignal(BaseModel):
    id: str
    junction_id: str
    lat: float
    lon: float
    # SUMO program: list of (state string, duration seconds)
    phases: list[dict[str, Any]] = Field(default_factory=list)
    cycle_time_s: float = 0.0
    controlled_lanes: list[str] = Field(default_factory=list)


class NetworkStats(BaseModel):
    junctions: int = 0
    signalised_junctions: int = 0
    segments: int = 0
    lane_km: float = 0.0
    total_length_km: float = 0.0
    roundabouts: int = 0
    build_seconds: float = 0.0
    # the acceleration claim, computed not asserted
    manual_effort_hours_estimate: float = 0.0


class RoadNetwork(BaseModel):
    """The digital twin of the road world. Produced once, simulated many times."""

    schema_version: str = SCHEMA_VERSION
    id: str
    name: str
    bbox: BBox
    junctions: list[Junction] = Field(default_factory=list)
    segments: list[RoadSegment] = Field(default_factory=list)
    signals: list[TrafficSignal] = Field(default_factory=list)
    stats: NetworkStats = Field(default_factory=NetworkStats)
    # path to the SUMO .net.xml on disk; the sim engine needs it, the UI does not
    sumo_net_path: Optional[str] = None


# --------------------------------------------------------------------------
# 2. SCENARIO
# --------------------------------------------------------------------------


class VehicleClass(str, Enum):
    """The Indian traffic mix. Each maps to a calibrated SUMO vType."""

    CAR = "car"
    MOTORCYCLE = "motorcycle"
    AUTO_RICKSHAW = "auto_rickshaw"
    BUS = "bus"
    TRUCK = "truck"
    BICYCLE = "bicycle"


# Default modal split for a typical Indian urban arterial.
# Two-wheeler dominance is the single most important calibration input.
DEFAULT_VEHICLE_MIX: dict[str, float] = {
    VehicleClass.MOTORCYCLE.value: 0.42,
    VehicleClass.CAR.value: 0.30,
    VehicleClass.AUTO_RICKSHAW.value: 0.14,
    VehicleClass.BUS.value: 0.05,
    VehicleClass.TRUCK.value: 0.04,
    VehicleClass.BICYCLE.value: 0.05,
}


class SignalStrategy(str, Enum):
    FIXED = "fixed"            # SUMO static program from OSM
    ACTUATED = "actuated"      # gap-based actuation
    ADAPTIVE = "adaptive"      # delay-based adaptive
    MAX_PRESSURE = "max_pressure"  # our TraCI max-pressure controller


class Weather(str, Enum):
    CLEAR = "clear"
    RAIN = "rain"
    HEAVY_RAIN = "heavy_rain"


class LaneClosure(BaseModel):
    """Close N lanes of a segment for a window. The core 'what-if'."""

    segment_id: str
    lanes_closed: int = 1
    start_s: float = 0.0
    end_s: float = 1e9


class Incident(BaseModel):
    """A stopped vehicle / breakdown at a point along a segment."""

    segment_id: str
    position: float = 0.5   # 0..1 along the segment
    duration_s: float = 300.0
    start_s: float = 0.0
    lane_index: int = 0


class Obstruction(BaseModel):
    """Static Indian-road obstruction: pothole, vendor, parked vehicle, works."""

    segment_id: str
    kind: Literal["pothole", "construction", "vendor", "parked_vehicle", "barricade"]
    position: float = 0.5
    severity: float = 0.5   # 0..1, scales the local speed/capacity penalty


class Scenario(BaseModel):
    """A question we ask of the twin. Fully declarative and validated."""

    schema_version: str = SCHEMA_VERSION
    id: str
    name: str = "Untitled scenario"
    description: str = ""

    duration_s: int = 900
    seed: int = 42

    demand_multiplier: float = Field(default=1.0, ge=0.0, le=10.0)
    vehicle_mix: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_VEHICLE_MIX))

    lane_closures: list[LaneClosure] = Field(default_factory=list)
    incidents: list[Incident] = Field(default_factory=list)
    obstructions: list[Obstruction] = Field(default_factory=list)

    signal_strategy: SignalStrategy = SignalStrategy.FIXED
    weather: Weather = Weather.CLEAR

    # Sublane model: the mechanism behind lateral weaving / filtering.
    lateral_resolution_m: float = 0.8
    lane_discipline: float = Field(default=0.35, ge=0.0, le=1.0)
    """0 = total free-for-all lateral movement, 1 = strict Western lane discipline."""

    # provenance: set when the scenario came from natural language
    source_prompt: Optional[str] = None


# --------------------------------------------------------------------------
# 3. SIMULATION RESULT
# --------------------------------------------------------------------------


class Metrics(BaseModel):
    """Headline numbers. These are what the judge reads off the screen."""

    avg_speed_kmh: float = 0.0
    avg_travel_time_s: float = 0.0
    avg_delay_s: float = 0.0
    max_queue_m: float = 0.0
    total_queue_m: float = 0.0
    throughput_veh_hr: float = 0.0
    vehicles_loaded: int = 0
    vehicles_arrived: int = 0
    vehicles_still_running: int = 0
    congestion_index: float = 0.0   # 0..1, 1 = gridlock
    total_co2_kg: float = 0.0
    total_fuel_l: float = 0.0
    teleports: int = 0              # gridlock indicator; honesty metric


class TimeseriesPoint(BaseModel):
    t: float
    mean_speed_kmh: float
    running: int
    arrived_cumulative: int
    halting: int
    mean_waiting_s: float


class SegmentMetric(BaseModel):
    """Per-segment outcome, used to paint the congestion heatmap."""

    segment_id: str
    mean_speed_kmh: float = 0.0
    free_flow_speed_kmh: float = 0.0
    speed_ratio: float = 1.0    # mean/free-flow -> the colour scale
    density_veh_km: float = 0.0
    max_queue_m: float = 0.0
    mean_waiting_s: float = 0.0
    vehicles_seen: int = 0


class Bottleneck(BaseModel):
    """A ranked problem location with an attributed cause."""

    rank: int
    segment_id: str
    junction_id: Optional[str] = None
    name: str = ""
    lat: float = 0.0
    lon: float = 0.0
    severity: float = 0.0       # 0..1
    speed_ratio: float = 1.0
    mean_waiting_s: float = 0.0
    queue_m: float = 0.0
    # attribution: {"lane reduction": 0.72, "two-wheeler density": 0.18, ...}
    causes: dict[str, float] = Field(default_factory=dict)


class VehicleFrame(BaseModel):
    """One vehicle at one instant. Compact by design -- these get big."""

    id: str
    c: str        # vehicle class shorthand
    lon: float
    lat: float
    a: float      # angle degrees
    s: float      # speed km/h


class PlaybackFrame(BaseModel):
    t: float
    vehicles: list[VehicleFrame] = Field(default_factory=list)


class RunStatus(str, Enum):
    QUEUED = "queued"
    BUILDING = "building"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class SimulationRun(BaseModel):
    """Everything produced by one execution of one Scenario on one RoadNetwork."""

    schema_version: str = SCHEMA_VERSION
    id: str
    network_id: str
    scenario: Scenario
    status: RunStatus = RunStatus.QUEUED
    progress: float = 0.0
    error: Optional[str] = None

    metrics: Metrics = Field(default_factory=Metrics)
    timeseries: list[TimeseriesPoint] = Field(default_factory=list)
    segment_metrics: list[SegmentMetric] = Field(default_factory=list)
    bottlenecks: list[Bottleneck] = Field(default_factory=list)
    playback: list[PlaybackFrame] = Field(default_factory=list)

    wall_seconds: float = 0.0
    sim_seconds: float = 0.0
    realtime_factor: float = 0.0


class Comparison(BaseModel):
    """Baseline vs scenario. The money slide."""

    baseline_run_id: str
    scenario_run_id: str
    baseline: Metrics
    scenario: Metrics
    deltas: dict[str, float] = Field(default_factory=dict)      # absolute
    deltas_pct: dict[str, float] = Field(default_factory=dict)  # percent
    verdict: str = ""
