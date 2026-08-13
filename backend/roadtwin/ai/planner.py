"""Natural language -> validated Scenario.

Architecture note, because this is the part reviewers are right to be sceptical
about: the language model NEVER touches the simulation. It only proposes a
Scenario, which is then validated by the same pydantic contract the UI uses.
An invalid or hallucinated field is rejected before SUMO ever starts.

Equally important, the default path uses no model at all. A deterministic
grammar handles the phrasings that actually occur ("close two lanes on 80 Feet
Road during peak hour", "what if it rains and traffic goes up 30%"). The LLM is
an optional enhancement for unusual phrasing, not a dependency -- so the demo
cannot be broken by a missing API key or a rate limit.
"""

from __future__ import annotations

import difflib
import re
import uuid

from ..contracts import (
    Incident,
    LaneClosure,
    Obstruction,
    RoadNetwork,
    Scenario,
    SignalStrategy,
    Weather,
)

# ---------------------------------------------------------------- patterns

_PERCENT_UP = re.compile(
    r"(?:increase|raise|up|more|higher|grow|surge|\+)\D{0,18}?(\d{1,3})\s*(?:%|percent)",
    re.I,
)
_PERCENT_DOWN = re.compile(
    r"(?:decrease|reduce|down|less|lower|drop|cut|\-)\D{0,18}?(\d{1,3})\s*(?:%|percent)",
    re.I,
)
_BARE_PERCENT = re.compile(r"(\d{1,3})\s*(?:%|percent)", re.I)
_MULTIPLIER = re.compile(r"(\d+(?:\.\d+)?)\s*(?:x|times)\s*(?:the\s*)?traffic", re.I)
_DURATION = re.compile(r"(\d{1,3})\s*(minute|min|hour|hr|second|sec)s?\b", re.I)
_LANE_COUNT = re.compile(
    r"(?:clos|block|shut|barricad)\w*\s+(?:the\s+|off\s+)?(\w+)\s+lane", re.I
)

_WORD_NUMBERS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "single": 1, "double": 2, "both": 2, "left": 1, "right": 1, "kerb": 1,
    "kerbside": 1, "curb": 1, "curbside": 1, "outer": 1, "inner": 1,
}

_PEAK_TERMS = ("peak", "rush hour", "rush-hour", "office hour", "morning peak", "evening peak")


def _extract_demand(text: str) -> tuple[float, list[str]]:
    """Resolve the demand multiplier and explain how it was derived."""
    notes: list[str] = []
    multiplier = 1.0

    match = _MULTIPLIER.search(text)
    if match:
        multiplier = float(match.group(1))
        notes.append(f"Demand set to {multiplier:g}x from an explicit multiplier.")
        return max(0.1, min(10.0, multiplier)), notes

    up = _PERCENT_UP.search(text)
    down = _PERCENT_DOWN.search(text)
    if up:
        multiplier = 1.0 + int(up.group(1)) / 100.0
        notes.append(f"Demand increased {up.group(1)}%.")
    elif down:
        multiplier = max(0.1, 1.0 - int(down.group(1)) / 100.0)
        notes.append(f"Demand reduced {down.group(1)}%.")
    else:
        bare = _BARE_PERCENT.search(text)
        if bare and re.search(r"traffic|volume|demand|flow", text, re.I):
            multiplier = 1.0 + int(bare.group(1)) / 100.0
            notes.append(f"Demand increased {bare.group(1)}% (inferred).")

    if any(term in text.lower() for term in _PEAK_TERMS) and multiplier == 1.0:
        multiplier = 1.35
        notes.append("Peak-hour conditions applied as a 1.35x demand factor.")
    elif any(term in text.lower() for term in _PEAK_TERMS):
        notes.append("Peak-hour phrasing detected alongside an explicit volume change.")

    return max(0.1, min(10.0, multiplier)), notes


def _extract_duration(text: str) -> tuple[int, list[str]]:
    match = _DURATION.search(text)
    if not match:
        return 900, []
    value, unit = int(match.group(1)), match.group(2).lower()
    seconds = value * (3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1)
    seconds = max(120, min(7200, seconds))
    return seconds, [f"Simulation horizon set to {seconds // 60} minutes."]


def _extract_weather(text: str) -> tuple[Weather, list[str]]:
    lowered = text.lower()
    # Matches both "heavy rain" and "rains heavily".
    if re.search(
        r"heav\w*\s+rain|rain\w*\s+heav\w*|downpour|monsoon|flood|storm|torrential",
        lowered,
    ):
        return Weather.HEAVY_RAIN, ["Heavy rain: reduced speeds and longer headways."]
    if re.search(r"\brain|wet|drizzle|shower", lowered):
        return Weather.RAIN, ["Rain: moderately reduced speeds."]
    return Weather.CLEAR, []


def _extract_strategy(text: str) -> tuple[SignalStrategy, list[str]]:
    lowered = text.lower()
    if re.search(r"max[- ]?pressure", lowered):
        return SignalStrategy.MAX_PRESSURE, ["Max-pressure signal control enabled."]
    if re.search(r"adaptive|smart signal|responsive signal|optimi[sz]e.{0,12}signal", lowered):
        return SignalStrategy.ADAPTIVE, ["Adaptive signal control enabled."]
    if re.search(r"actuated", lowered):
        return SignalStrategy.ACTUATED, ["Actuated signal control enabled."]
    return SignalStrategy.FIXED, []


def _match_segments(
    text: str, network: RoadNetwork, limit: int = 4
) -> tuple[list[str], str | None]:
    """Find the road the user named, by fuzzy-matching against segment names."""
    named: dict[str, list[str]] = {}
    for segment in network.segments:
        if segment.name:
            named.setdefault(segment.name.lower(), []).append(segment.id)
    if not named:
        return [], None

    lowered = text.lower()
    # Direct substring hit is the strongest signal.
    hits = [name for name in named if len(name) > 4 and name in lowered]
    if not hits:
        # Otherwise fuzzy-match the longest capitalised phrase in the prompt.
        phrases = re.findall(
            r"\b((?:[A-Z][\w']*|\d+)(?:\s+(?:[A-Z][\w']*|\d+|Road|Main|Cross|Marg))+)\b", text
        )
        for phrase in sorted(phrases, key=len, reverse=True):
            close = difflib.get_close_matches(phrase.lower(), list(named), n=1, cutoff=0.72)
            if close:
                hits = close
                break
    if not hits:
        return [], None

    best = max(hits, key=len)
    # Longest segments of that road: closing a 6 m stub proves nothing.
    lengths = {s.id: s.length_m for s in network.segments}
    ids = sorted(named[best], key=lambda i: -lengths.get(i, 0))[:limit]
    return ids, best


def parse_scenario(text: str, network: RoadNetwork) -> tuple[Scenario, list[str]]:
    """Deterministically convert a request into a validated Scenario.

    Returns the Scenario plus a human-readable explanation of every inference,
    so the user can see exactly how their words became simulation parameters.
    """
    explanation: list[str] = []

    demand, notes = _extract_demand(text)
    explanation += notes
    duration, notes = _extract_duration(text)
    explanation += notes
    weather, notes = _extract_weather(text)
    explanation += notes
    strategy, notes = _extract_strategy(text)
    explanation += notes

    segment_ids, road_name = _match_segments(text, network)
    if road_name:
        explanation.append(
            f"Matched '{road_name.title()}' to {len(segment_ids)} network segment(s)."
        )

    closures: list[LaneClosure] = []
    incidents: list[Incident] = []
    obstructions: list[Obstruction] = []

    lowered = text.lower()
    wants_closure = bool(re.search(r"clos|block|shut|barricad|construction|road ?work", lowered))
    if wants_closure:
        lane_match = _LANE_COUNT.search(text)
        lanes = _WORD_NUMBERS.get((lane_match.group(1).lower() if lane_match else "one"), 1)
        targets = segment_ids or _busiest_fallback(network)
        for segment_id in targets:
            closures.append(LaneClosure(segment_id=segment_id, lanes_closed=lanes))
        explanation.append(
            f"Closing {lanes} lane(s) on {len(targets)} segment(s)"
            + ("." if road_name else " (no road named: applied to the busiest corridor).")
        )

    if re.search(r"accident|crash|collision|breakdown|stalled", lowered):
        targets = segment_ids or _busiest_fallback(network, limit=1)
        for segment_id in targets[:1]:
            incidents.append(Incident(segment_id=segment_id, duration_s=min(duration, 600)))
        explanation.append("Incident placed mid-segment as a stopped vehicle.")

    for kind, pattern in (
        ("pothole", r"pothole|bad road|broken road"),
        ("construction", r"construction|road ?work|excavation|digging"),
        ("vendor", r"vendor|hawker|stall|market"),
        ("parked_vehicle", r"parked|parking"),
        ("barricade", r"barricad|diversion"),
    ):
        if re.search(pattern, lowered):
            targets = segment_ids or _busiest_fallback(network, limit=2)
            for segment_id in targets[:2]:
                obstructions.append(
                    Obstruction(segment_id=segment_id, kind=kind, severity=0.6)  # type: ignore[arg-type]
                )
            explanation.append(f"Added {kind} obstruction(s).")

    # Lane discipline: let the user dial the sublane behaviour verbally.
    discipline = 0.35
    if re.search(r"discipline|orderly|lane[- ]abiding|strict lane", lowered):
        discipline = 0.9
        explanation.append("Strict lane discipline applied (Western-style lane following).")
    elif re.search(r"chaotic|undisciplined|free[- ]for[- ]all|no lane", lowered):
        discipline = 0.1
        explanation.append("Minimal lane discipline applied (heavy lateral filtering).")

    if not explanation:
        explanation.append("No specific conditions recognised; running a baseline scenario.")

    scenario = Scenario(
        id=f"sc_{uuid.uuid4().hex[:8]}",
        name=_name_for(text),
        description=text.strip()[:280],
        duration_s=duration,
        demand_multiplier=demand,
        lane_closures=closures,
        incidents=incidents,
        obstructions=obstructions,
        signal_strategy=strategy,
        weather=weather,
        lane_discipline=discipline,
        source_prompt=text.strip()[:500],
    )
    return scenario, explanation


def _busiest_fallback(network: RoadNetwork, limit: int = 3) -> list[str]:
    """When no road is named, target the highest-capacity corridor available."""
    ranked = sorted(
        network.segments,
        key=lambda s: (s.lanes, s.length_m, s.speed_limit_kmh),
        reverse=True,
    )
    return [s.id for s in ranked[:limit]]


def _name_for(text: str) -> str:
    cleaned = " ".join(text.strip().split())
    if len(cleaned) <= 48:
        return cleaned or "Custom scenario"
    return cleaned[:45].rstrip() + "..."
