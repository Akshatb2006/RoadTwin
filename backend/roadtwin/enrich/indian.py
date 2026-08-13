"""Indian road semantics enrichment.

OSM tells you a road exists. It does not tell you that 1.2 m of its kerbside
lane is permanently occupied by parked two-wheelers and a tea stall, which is
the difference between the modelled capacity and the observed one.

This module derives, per segment:
  * effective_lanes   -- usable width after encroachment, not nominal lanes
  * capacity_pcu_hr   -- IRC-style capacity, adjusted for the local mix
  * encroachment      -- fraction of carriageway lost to static obstruction
  * two_wheeler_share -- expected 2W share, which varies strongly by road class

Everything here is a transparent, tunable heuristic driven by attributes we
actually have (road class, lane count, speed, junction density). It is not
presented as ground truth: it is a defensible prior that a video-calibration
pass can later replace with measured values. The alternative -- assuming Indian
roads behave like the IRC design standard -- is reliably wrong in one direction.
"""

from __future__ import annotations

from ..contracts import (
    JunctionType,
    RoadClass,
    RoadNetwork,
    RoadSegment,
    SurfaceQuality,
)

# Base capacity in PCU/hr per lane, by road class (IRC:106 urban roads).
BASE_CAPACITY_PCU_PER_LANE: dict[RoadClass, float] = {
    RoadClass.MOTORWAY: 2000.0,
    RoadClass.TRUNK: 1800.0,
    RoadClass.ARTERIAL: 1600.0,
    RoadClass.SUB_ARTERIAL: 1400.0,
    RoadClass.COLLECTOR: 1200.0,
    RoadClass.LOCAL: 900.0,
    RoadClass.SERVICE: 600.0,
    RoadClass.LINK: 1400.0,
    RoadClass.OTHER: 900.0,
}

# Typical share of carriageway width lost to parking, vendors and encroachment.
# Local and service roads suffer most; access-controlled roads least.
BASE_ENCROACHMENT: dict[RoadClass, float] = {
    RoadClass.MOTORWAY: 0.00,
    RoadClass.TRUNK: 0.04,
    RoadClass.ARTERIAL: 0.12,
    RoadClass.SUB_ARTERIAL: 0.18,
    RoadClass.COLLECTOR: 0.24,
    RoadClass.LOCAL: 0.30,
    RoadClass.SERVICE: 0.34,
    RoadClass.LINK: 0.06,
    RoadClass.OTHER: 0.20,
}

# Two-wheeler share rises sharply on smaller roads, where filtering pays off.
BASE_TWO_WHEELER_SHARE: dict[RoadClass, float] = {
    RoadClass.MOTORWAY: 0.22,
    RoadClass.TRUNK: 0.30,
    RoadClass.ARTERIAL: 0.38,
    RoadClass.SUB_ARTERIAL: 0.44,
    RoadClass.COLLECTOR: 0.50,
    RoadClass.LOCAL: 0.56,
    RoadClass.SERVICE: 0.58,
    RoadClass.LINK: 0.34,
    RoadClass.OTHER: 0.45,
}


def _surface_for(segment: RoadSegment) -> SurfaceQuality:
    """Infer surface quality. Low design speed on a small road is a strong
    proxy for poor riding quality in the absence of a surface tag."""
    if segment.road_class in (RoadClass.MOTORWAY, RoadClass.TRUNK):
        return SurfaceQuality.GOOD
    if segment.road_class in (RoadClass.ARTERIAL, RoadClass.SUB_ARTERIAL):
        return SurfaceQuality.GOOD if segment.speed_limit_kmh >= 45 else SurfaceQuality.FAIR
    if segment.road_class in (RoadClass.SERVICE, RoadClass.LOCAL):
        return SurfaceQuality.POOR if segment.speed_limit_kmh <= 25 else SurfaceQuality.FAIR
    return SurfaceQuality.FAIR


def enrich_network(network: RoadNetwork) -> RoadNetwork:
    """Populate the Indian-context fields on every segment. Mutates in place."""
    junctions = {j.id: j for j in network.junctions}

    # Junction density per segment drives side-friction: more approaches and
    # more nearby entries means more merging conflict and lower usable capacity.
    for segment in network.segments:
        road_class = segment.road_class
        notes: list[str] = []

        encroachment = BASE_ENCROACHMENT.get(road_class, 0.2)

        # Short blocks between junctions imply frequent conflict points.
        if segment.length_m < 60:
            encroachment += 0.06
            notes.append("Short block: frequent conflict points")

        downstream = junctions.get(segment.to_junction)
        if downstream and downstream.degree >= 5:
            encroachment += 0.05
            notes.append("Complex downstream junction")

        # A signalised approach accumulates stopped vehicles and hawkers.
        if downstream and downstream.type is JunctionType.SIGNALISED:
            notes.append("Signalised approach")

        if downstream and downstream.type is JunctionType.ROUNDABOUT:
            notes.append("Roundabout approach: weaving section")

        encroachment = round(min(0.55, encroachment), 3)

        # Effective lanes: nominal capacity minus encroachment. Never below the
        # width of a single usable lane, because traffic always finds a way.
        effective = max(0.6, segment.lanes * (1.0 - encroachment))

        two_wheeler = BASE_TWO_WHEELER_SHARE.get(road_class, 0.45)
        # Two-wheelers partially recover lost width -- they use the residual
        # gaps that a car cannot. This is the mechanism that keeps Indian roads
        # moving at densities where a car-only fleet would be gridlocked.
        recovery = 1.0 + 0.45 * two_wheeler * encroachment * 2.0

        base_capacity = BASE_CAPACITY_PCU_PER_LANE.get(road_class, 900.0)
        capacity = base_capacity * effective * recovery

        surface = _surface_for(segment)
        if surface is SurfaceQuality.POOR:
            capacity *= 0.82
            notes.append("Poor surface: reduced speed and capacity")
        elif surface is SurfaceQuality.FAIR:
            capacity *= 0.93

        segment.encroachment = encroachment
        segment.effective_lanes = round(effective, 2)
        segment.two_wheeler_share = round(two_wheeler, 3)
        segment.capacity_pcu_hr = round(capacity, 0)
        segment.surface = surface
        segment.notes = notes

    return network


def network_summary(network: RoadNetwork) -> dict:
    """Headline enrichment figures for the UI's 'digital twin generated' panel."""
    segments = network.segments
    if not segments:
        return {}

    total_nominal = sum(s.lanes * s.length_m for s in segments)
    total_effective = sum(s.effective_lanes * s.length_m for s in segments)
    capacity_loss = 1.0 - (total_effective / total_nominal) if total_nominal else 0.0

    by_class: dict[str, int] = {}
    for segment in segments:
        by_class[segment.road_class.value] = by_class.get(segment.road_class.value, 0) + 1

    poor = sum(1 for s in segments if s.surface is SurfaceQuality.POOR)
    return {
        "segments_enriched": len(segments),
        "mean_encroachment": round(
            sum(s.encroachment for s in segments) / len(segments), 3
        ),
        "capacity_lost_to_encroachment_pct": round(capacity_loss * 100, 1),
        "mean_effective_lanes": round(
            sum(s.effective_lanes for s in segments) / len(segments), 2
        ),
        "total_capacity_pcu_hr": round(sum(s.capacity_pcu_hr for s in segments), 0),
        "poor_surface_segments": poor,
        "road_class_mix": dict(sorted(by_class.items(), key=lambda kv: -kv[1])),
    }
