"""Traffic signal control strategies, driven live over TraCI.

The baseline (FIXED) is whatever fixed-time plan netconvert synthesised from
OSM. The other strategies re-time signals in the loop using real queue state,
which is what lets the platform answer "would adaptive control help here?"
with a simulated number instead of an opinion.

MAX_PRESSURE is the interesting one: it is a decentralised policy with a proven
stability guarantee (it maximises network throughput without needing demand
estimates). Pressure for a phase is the difference between upstream queue and
downstream queue over the movements that phase serves; serving the highest-
pressure phase provably keeps the network's queues bounded whenever any policy
can.
"""

from __future__ import annotations

from ..contracts import SignalStrategy


class SignalController:
    """Base: fixed-time. Does nothing, letting SUMO run the static program."""

    def __init__(self, traci_conn, tls_ids: list[str]):
        self.traci = traci_conn
        self.tls_ids = tls_ids

    def step(self, t: float) -> None:  # noqa: ARG002
        return


class MaxPressureController(SignalController):
    """Decentralised max-pressure control with a minimum-green constraint."""

    MIN_GREEN_S = 12.0
    DECISION_INTERVAL_S = 5.0
    AMBER_S = 4.0
    # Pressure must beat the incumbent by this margin to justify a switch.
    # Without hysteresis the controller thrashes between near-equal phases once
    # every approach is saturated, and each switch costs a clearance interval.
    SWITCH_MARGIN = 4.0

    def __init__(self, traci_conn, tls_ids: list[str]):
        super().__init__(traci_conn, tls_ids)
        self._last_switch: dict[str, float] = {t: 0.0 for t in tls_ids}
        # tls_id -> (target_phase, time_at_which_amber_ends)
        self._pending: dict[str, tuple[int, float]] = {}
        self._last_decision = -1e9
        # Cache the static structure once: phases and their controlled links.
        self._phases: dict[str, list] = {}
        self._links: dict[str, list] = {}
        for tls_id in tls_ids:
            try:
                logic = traci_conn.trafficlight.getAllProgramLogics(tls_id)[0]
                self._phases[tls_id] = list(logic.phases)
                self._links[tls_id] = traci_conn.trafficlight.getControlledLinks(tls_id)
            except Exception:  # noqa: BLE001 - skip malformed junctions
                continue

    def _pressure(self, tls_id: str, phase_index: int) -> float:
        """Sum of (upstream queue - downstream queue) over links this phase serves."""
        phases = self._phases.get(tls_id) or []
        links = self._links.get(tls_id) or []
        if phase_index >= len(phases):
            return -1e9
        state = phases[phase_index].state
        total = 0.0
        for link_index, signal_char in enumerate(state):
            if signal_char not in ("G", "g"):
                continue
            if link_index >= len(links) or not links[link_index]:
                continue
            in_lane, out_lane, _via = links[link_index][0]
            try:
                upstream = self.traci.lane.getLastStepHaltingNumber(in_lane)
                downstream = self.traci.lane.getLastStepHaltingNumber(out_lane)
            except Exception:  # noqa: BLE001
                continue
            total += upstream - downstream
        return total

    def _amber_after(self, tls_id: str, phase_index: int) -> int | None:
        """The amber interval that clears `phase_index`, if the program has one."""
        phases = self._phases.get(tls_id) or []
        for offset in range(1, len(phases)):
            candidate = (phase_index + offset) % len(phases)
            if "y" in phases[candidate].state.lower():
                return candidate
        return None

    def step(self, t: float) -> None:
        # Complete any amber clearance already in progress before deciding again.
        for tls_id, (target, ready_at) in list(self._pending.items()):
            if t >= ready_at:
                try:
                    self.traci.trafficlight.setPhase(tls_id, target)
                    self._last_switch[tls_id] = t
                except Exception:  # noqa: BLE001
                    pass
                self._pending.pop(tls_id, None)

        if t - self._last_decision < self.DECISION_INTERVAL_S:
            return
        self._last_decision = t

        for tls_id in self.tls_ids:
            phases = self._phases.get(tls_id)
            if not phases or tls_id in self._pending:
                continue
            if t - self._last_switch.get(tls_id, 0.0) < self.MIN_GREEN_S:
                continue
            try:
                current = self.traci.trafficlight.getPhase(tls_id)
            except Exception:  # noqa: BLE001
                continue

            # Only consider green phases; amber phases are transitions.
            candidates = [
                i for i, p in enumerate(phases)
                if "y" not in p.state.lower() and ("G" in p.state or "g" in p.state)
            ]
            if len(candidates) < 2 or current not in candidates:
                continue

            best = max(candidates, key=lambda i: self._pressure(tls_id, i))
            if best == current:
                continue
            if self._pressure(tls_id, best) <= self._pressure(tls_id, current) + self.SWITCH_MARGIN:
                continue

            # Never jump straight between conflicting greens: run the program's
            # amber first. Skipping it removes the clearance interval, so the
            # junction locks up and max-pressure ends up *worse* than fixed-time.
            amber = self._amber_after(tls_id, current)
            try:
                if amber is None:
                    self.traci.trafficlight.setPhase(tls_id, best)
                    self._last_switch[tls_id] = t
                else:
                    self.traci.trafficlight.setPhase(tls_id, amber)
                    self._pending[tls_id] = (best, t + self.AMBER_S)
            except Exception:  # noqa: BLE001
                continue


class AdaptiveController(SignalController):
    """Queue-responsive green extension / early termination.

    Simpler and more conservative than max-pressure: keep the current green if
    its approaches are still discharging, cut it short when they are empty.
    """

    MIN_GREEN_S = 8.0
    MAX_GREEN_S = 60.0
    DECISION_INTERVAL_S = 2.0

    def __init__(self, traci_conn, tls_ids: list[str]):
        super().__init__(traci_conn, tls_ids)
        self._green_since: dict[str, float] = {t: 0.0 for t in tls_ids}
        self._last_phase: dict[str, int] = {}
        self._last_decision = -1e9
        self._links: dict[str, list] = {}
        for tls_id in tls_ids:
            try:
                self._links[tls_id] = traci_conn.trafficlight.getControlledLinks(tls_id)
            except Exception:  # noqa: BLE001
                continue

    def step(self, t: float) -> None:
        if t - self._last_decision < self.DECISION_INTERVAL_S:
            return
        self._last_decision = t

        for tls_id in self.tls_ids:
            try:
                phase = self.traci.trafficlight.getPhase(tls_id)
                state = self.traci.trafficlight.getRedYellowGreenState(tls_id)
            except Exception:  # noqa: BLE001
                continue

            if self._last_phase.get(tls_id) != phase:
                self._last_phase[tls_id] = phase
                self._green_since[tls_id] = t
                continue

            if "y" in state.lower():
                continue  # never interrupt an amber interval

            elapsed = t - self._green_since.get(tls_id, t)
            if elapsed < self.MIN_GREEN_S:
                continue

            links = self._links.get(tls_id) or []
            waiting = 0
            for link_index, signal_char in enumerate(state):
                if signal_char not in ("G", "g"):
                    continue
                if link_index >= len(links) or not links[link_index]:
                    continue
                in_lane = links[link_index][0][0]
                try:
                    waiting += self.traci.lane.getLastStepHaltingNumber(in_lane)
                except Exception:  # noqa: BLE001
                    continue

            # Green is idle, or has run long enough -- hand it to the next phase.
            if waiting == 0 or elapsed > self.MAX_GREEN_S:
                try:
                    self.traci.trafficlight.setPhase(
                        tls_id, (phase + 1) % max(1, self._phase_count(tls_id))
                    )
                    self._green_since[tls_id] = t
                except Exception:  # noqa: BLE001
                    continue

    def _phase_count(self, tls_id: str) -> int:
        try:
            return len(self.traci.trafficlight.getAllProgramLogics(tls_id)[0].phases)
        except Exception:  # noqa: BLE001
            return 1


def make_controller(strategy: SignalStrategy, traci_conn, tls_ids: list[str]) -> SignalController:
    if strategy is SignalStrategy.MAX_PRESSURE:
        return MaxPressureController(traci_conn, tls_ids)
    if strategy is SignalStrategy.ADAPTIVE:
        return AdaptiveController(traci_conn, tls_ids)
    # ACTUATED is handled natively by SUMO's tls type; FIXED is the static plan.
    return SignalController(traci_conn, tls_ids)
