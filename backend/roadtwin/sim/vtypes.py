"""Calibrated vehicle types for Indian heterogeneous traffic.

This module is where "Indian traffic" stops being a slogan and becomes physics.

Two mechanisms do the heavy lifting:

1. SUBLANE MODEL. SUMO's sublane model discretises each lane laterally, so a
   vehicle occupies a continuous lateral position rather than a lane index. A
   0.8 m motorcycle can therefore share a 3.2 m lane with a car and filter
   through stopped traffic. This is the single most important difference
   between Indian and Western traffic microsimulation, and it is why a
   lane-based model badly over-predicts congestion here.

2. LANE DISCIPLINE. A scalar 0..1 that continuously interpolates the lateral
   aggression parameters (minGapLat, lcSublane, lcPushy, lcAssertive). At 1.0
   you get orderly Western lane-following; at 0.0 you get a free-for-all.

Dimensions are drawn from Indian Roads Congress vehicle classes; PCU factors
follow IRC:106 for urban roads.
"""

from __future__ import annotations

from xml.sax.saxutils import escape

from ..contracts import VehicleClass, Weather

# ---------------------------------------------------------------------------
# Base physical + behavioural parameters per class.
# PCU = passenger car units (IRC:106), used for capacity accounting.
# ---------------------------------------------------------------------------
VEHICLE_SPECS: dict[str, dict] = {
    VehicleClass.CAR.value: {
        "vClass": "passenger",
        "length": 4.2, "width": 1.75, "height": 1.5,
        "accel": 2.6, "decel": 4.5, "emergencyDecel": 9.0,
        "maxSpeed": 22.2,          # 80 km/h
        "minGap": 2.0, "sigma": 0.5, "tau": 1.0,
        "pcu": 1.0, "colour": "0.90,0.90,0.92",
        "emissionClass": "HBEFA3/PC_G_EU4",
    },
    VehicleClass.MOTORCYCLE.value: {
        "vClass": "motorcycle",
        "length": 1.9, "width": 0.75, "height": 1.4,
        "accel": 3.2, "decel": 5.5, "emergencyDecel": 9.0,
        "maxSpeed": 19.4,          # 70 km/h
        "minGap": 0.9, "sigma": 0.7, "tau": 0.7,
        "pcu": 0.25, "colour": "1.00,0.45,0.10",
        "emissionClass": "HBEFA3/PC_G_EU4",
    },
    VehicleClass.AUTO_RICKSHAW.value: {
        "vClass": "passenger",
        "length": 2.8, "width": 1.4, "height": 1.7,
        "accel": 2.0, "decel": 4.0, "emergencyDecel": 7.0,
        "maxSpeed": 13.9,          # 50 km/h
        "minGap": 1.2, "sigma": 0.8, "tau": 0.8,
        "pcu": 0.5, "colour": "1.00,0.85,0.10",
        "emissionClass": "HBEFA3/PC_G_EU4",
    },
    VehicleClass.BUS.value: {
        "vClass": "bus",
        "length": 11.0, "width": 2.5, "height": 3.2,
        "accel": 1.2, "decel": 3.5, "emergencyDecel": 6.0,
        "maxSpeed": 16.7,          # 60 km/h
        "minGap": 2.8, "sigma": 0.4, "tau": 1.3,
        "pcu": 3.0, "colour": "0.15,0.55,0.95",
        "emissionClass": "HBEFA3/Bus",
    },
    VehicleClass.TRUCK.value: {
        "vClass": "truck",
        "length": 7.5, "width": 2.4, "height": 3.0,
        "accel": 1.1, "decel": 3.5, "emergencyDecel": 6.0,
        "maxSpeed": 15.3,          # 55 km/h
        "minGap": 3.0, "sigma": 0.4, "tau": 1.4,
        "pcu": 3.0, "colour": "0.55,0.35,0.20",
        "emissionClass": "HBEFA3/HDV",
    },
    VehicleClass.BICYCLE.value: {
        "vClass": "bicycle",
        "length": 1.7, "width": 0.6, "height": 1.6,
        "accel": 1.0, "decel": 3.0, "emergencyDecel": 5.0,
        "maxSpeed": 5.6,           # 20 km/h
        "minGap": 0.6, "sigma": 0.8, "tau": 0.9,
        "pcu": 0.2, "colour": "0.40,0.85,0.40",
        "emissionClass": "HBEFA3/zero",
    },
}

# How far each class deviates from strict lane discipline. Motorcycles and autos
# filter hardest; buses and trucks physically cannot.
LATERAL_AGILITY: dict[str, float] = {
    VehicleClass.MOTORCYCLE.value: 1.00,
    VehicleClass.BICYCLE.value: 0.85,
    VehicleClass.AUTO_RICKSHAW.value: 0.75,
    VehicleClass.CAR.value: 0.45,
    VehicleClass.BUS.value: 0.12,
    VehicleClass.TRUCK.value: 0.10,
}

# Weather degrades speed and stretches following gaps.
WEATHER_EFFECTS: dict[str, dict[str, float]] = {
    Weather.CLEAR.value:      {"speed_factor": 1.00, "tau_scale": 1.00, "decel_scale": 1.00},
    Weather.RAIN.value:       {"speed_factor": 0.85, "tau_scale": 1.25, "decel_scale": 0.85},
    Weather.HEAVY_RAIN.value: {"speed_factor": 0.68, "tau_scale": 1.55, "decel_scale": 0.70},
}


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def build_vtype_xml(
    vehicle_mix: dict[str, float],
    lane_discipline: float = 0.35,
    weather: str = Weather.CLEAR.value,
) -> str:
    """Render a SUMO additional-file containing the calibrated vType distribution.

    `lane_discipline` 0..1 interpolates every lateral parameter, so a single
    slider in the UI moves the whole fleet between "orderly" and "Indian".
    """
    effects = WEATHER_EFFECTS.get(weather, WEATHER_EFFECTS[Weather.CLEAR.value])
    total = sum(v for v in vehicle_mix.values() if v > 0) or 1.0

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<additional>",
        '  <vTypeDistribution id="indian_mix">',
    ]

    for cls, share in vehicle_mix.items():
        spec = VEHICLE_SPECS.get(cls)
        if spec is None or share <= 0:
            continue
        probability = share / total
        agility = LATERAL_AGILITY.get(cls, 0.4)

        # Indiscipline is how far this class is willing to depart from lane
        # centre, given both the global slider and its own physical agility.
        indiscipline = (1.0 - lane_discipline) * agility

        min_gap_lat = _lerp(0.60, 0.08, indiscipline)   # m of lateral clearance
        lc_sublane = _lerp(1.0, 12.0, indiscipline)     # eagerness to move laterally
        lc_pushy = _lerp(0.0, 0.85, indiscipline)
        lc_assertive = _lerp(1.0, 3.0, indiscipline)
        lc_impatience = _lerp(0.0, 0.9, indiscipline)
        max_speed_lat = _lerp(0.7, 2.2, indiscipline)
        lc_speed_gain = _lerp(1.0, 4.0, indiscipline)
        # Motorcycles align to the right kerb-side edge to filter; others centre.
        lat_alignment = "arbitrary" if indiscipline > 0.45 else "center"

        tau = spec["tau"] * effects["tau_scale"]
        decel = spec["decel"] * effects["decel_scale"]
        speed_factor = effects["speed_factor"]

        lines.append(
            f'    <vType id="{escape(cls)}" probability="{probability:.4f}" '
            f'vClass="{spec["vClass"]}" guiShape="{_gui_shape(cls)}" '
            f'length="{spec["length"]}" width="{spec["width"]}" height="{spec["height"]}" '
            f'accel="{spec["accel"]}" decel="{decel:.2f}" '
            f'emergencyDecel="{spec["emergencyDecel"]}" '
            f'maxSpeed="{spec["maxSpeed"]}" minGap="{spec["minGap"]}" '
            f'sigma="{spec["sigma"]}" tau="{tau:.2f}" '
            f'speedFactor="normc({speed_factor:.2f},0.12,0.5,1.4)" '
            f'color="{spec["colour"]}" emissionClass="{spec["emissionClass"]}" '
            f'carFollowModel="Krauss" laneChangeModel="SL2015" '
            f'latAlignment="{lat_alignment}" minGapLat="{min_gap_lat:.2f}" '
            f'maxSpeedLat="{max_speed_lat:.2f}" '
            f'lcSublane="{lc_sublane:.2f}" lcPushy="{lc_pushy:.2f}" '
            f'lcAssertive="{lc_assertive:.2f}" lcImpatience="{lc_impatience:.2f}" '
            f'lcSpeedGain="{lc_speed_gain:.2f}" lcKeepRight="0.0" '
            f'jmDriveAfterRedTime="{_jm_red(indiscipline):.1f}" '
            f'jmIgnoreFoeProb="{_jm_ignore(indiscipline):.2f}" '
            f'jmIgnoreFoeSpeed="6.0" jmCrossingGap="{_lerp(10.0, 1.5, indiscipline):.1f}" '
            f'impatience="{_lerp(0.1, 0.9, indiscipline):.2f}" />'
        )

    lines.append("  </vTypeDistribution>")
    lines.append("</additional>")
    return "\n".join(lines)


def _gui_shape(cls: str) -> str:
    return {
        VehicleClass.CAR.value: "passenger",
        VehicleClass.MOTORCYCLE.value: "motorcycle",
        VehicleClass.AUTO_RICKSHAW.value: "passenger/van",
        VehicleClass.BUS.value: "bus",
        VehicleClass.TRUCK.value: "truck",
        VehicleClass.BICYCLE.value: "bicycle",
    }.get(cls, "passenger")


def _jm_red(indiscipline: float) -> float:
    """Seconds after a red onset during which a driver will still proceed.

    Amber/red-running is endemic at Indian junctions and materially changes
    junction capacity, so we model it explicitly rather than pretending it away.
    SUMO requires this to be >= 0, so a disciplined fleet sits at exactly 0.
    """
    return max(0.0, _lerp(0.0, 3.0, indiscipline))


def _jm_ignore(indiscipline: float) -> float:
    """Probability of ignoring a right-of-way foe when gaps are tight."""
    return _lerp(0.0, 0.35, indiscipline)


def pcu_of(cls: str) -> float:
    spec = VEHICLE_SPECS.get(cls)
    return float(spec["pcu"]) if spec else 1.0


def mix_pcu_factor(vehicle_mix: dict[str, float]) -> float:
    """Average PCU per vehicle for a mix -- converts vehicle counts to PCU."""
    total = sum(v for v in vehicle_mix.values() if v > 0) or 1.0
    return sum(pcu_of(c) * s for c, s in vehicle_mix.items() if s > 0) / total
