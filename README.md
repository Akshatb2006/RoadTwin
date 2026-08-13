# RoadTwin

**Automated high-fidelity road network modelling for Indian traffic simulations.**
SIH25100 · MathWorks · Transportation & Logistics

Select an Indian city area → a simulation-ready digital twin is generated in
seconds → run what-if scenarios on it → see what changed and *why*.

---

## The problem, precisely

Preparing a road network for microscopic traffic simulation is the expensive
part of traffic engineering. Junction geometry, turn lanes, connection
topology, right-of-way and signal plans are normally coded by hand, at roughly
half an hour per junction. A 1,000-junction study area is therefore months of
work before a single vehicle moves.

RoadTwin compresses that to seconds, and then makes the resulting model
*useful* — because a network that is merely geometrically correct still
mispredicts Indian traffic badly.

## What we built vs. what we stand on

We deliberately do not reimplement traffic physics. SUMO (Eclipse, EPL-2.0) is
a mature microscopic simulator; `netconvert` encodes decades of junction
reconstruction logic. Reimplementing either in a hackathon would produce a
worse result.

| Layer | Source |
|---|---|
| Microscopic traffic simulation, routing, car-following | SUMO |
| OSM → junction/turn-lane/signal reconstruction | `netconvert` |
| Live simulation control | TraCI |
| **Indian-context calibration & sublane fleet model** | **ours** |
| **Road semantics enrichment (effective capacity)** | **ours** |
| **Scenario abstraction + validated NL interface** | **ours** |
| **Bottleneck detection & causal attribution** | **ours** |
| **Parallel scenario execution & comparison** | **ours** |
| **Max-pressure / adaptive signal controllers** | **ours** |

## Why Indian roads need more than a geometric twin

Two mechanisms carry most of the fidelity, and both are real modelling
decisions rather than cosmetics:

**1. The sublane model.** A lane-based simulator forces a 0.75 m motorcycle to
occupy a full 3.2 m lane. That single assumption makes a lane-based model
over-predict congestion severely here, because it cannot represent filtering.
We enable SUMO's sublane model and calibrate a six-class fleet (car,
motorcycle, auto-rickshaw, bus, truck, bicycle) with per-class lateral
agility, gap acceptance and amber-running behaviour.

A single **lane discipline** slider (0–1) interpolates every lateral parameter
across the fleet, so you can watch the same demand behave as orderly Western
lane-following or as Indian lateral filtering. In our runs, *strict lane
discipline is worse* — it serves fewer vehicles at the same demand.

**2. Effective capacity, not nominal capacity.** OSM says a road has two lanes.
It does not say that kerbside parking, vendors and encroachment have removed a
third of the usable width. We derive `effective_lanes`, `encroachment` and
IRC-style `capacity_pcu_hr` per segment. Across Koramangala this reclassifies
**~33% of nominal capacity** as unavailable.

`--lefthand` is passed to `netconvert`: India drives on the left, and without
it every junction's turn geometry and right-of-way is silently mirrored.

## Honest engineering notes

- **The LLM never runs the simulation.** Natural language is parsed into a
  `Scenario`, validated by the same pydantic contract the UI uses, and only
  then executed. The default path uses a deterministic grammar and **no API
  key**, so the demo cannot be broken by a rate limit.
- **Attribution is computed, not generated.** Bottleneck causes come from
  simulation state (closures, downstream signal waiting, congested successors),
  not from asking a model to speculate. An LLM asked "why is this congested?"
  always produces a fluent answer, including when it is wrong.
- **Teleports are reported, not hidden.** SUMO teleports gridlocked vehicles;
  we surface the count as a severity signal rather than quietly absorbing it.
- **"Actuated" signal control is deliberately not exposed** — our OSM-derived
  plans are static, so it would silently reproduce the fixed-time result.
- The manual-effort figure is an **estimate** with stated assumptions
  (0.5 h/junction + 0.2 h/km), shown as a formula, not a marketing number.

## Two environment pitfalls (both cost real debugging time)

**`maplibre-gl` is pinned to v5 on purpose.** v6 is ESM-only and loads its
worker from a separate `maplibre-gl-worker.mjs`, which Turbopack does not
resolve. The worker then fails to start *silently*: no error event, no console
message — sources never finish loading, `isStyleLoaded()` stays false forever,
and the map renders an empty canvas over a correctly-positioned camera. Do not
upgrade without checking that roads still draw.

**`allowedDevOrigins` in `next.config.ts` is required.** Next's dev server 403s
every `/_next/*` chunk when the page is opened from a host it does not consider
same-origin — which includes reaching your own machine as `127.0.0.1` instead
of `localhost`, or over the LAN. The 403 body is HTML, so the browser reports
it as *"Failed to load module script: non-JavaScript MIME type"*. curl will
happily return 200 for the same URL because it sends no `Origin` header.

## Architecture

```
 Study area ──► Overpass ──► netconvert ──► RoadNetwork ──► Indian enrichment
                                                │
 Natural language ──► deterministic parser ──► Scenario (validated)
                                                │
                                    ProcessPool ├─► SUMO + TraCI ─┐
                                    (N workers) ├─► SUMO + TraCI ─┤
                                                └─► SUMO + TraCI ─┘
                                                                  │
                        metrics · timeseries · per-segment · bottlenecks · playback
```

Three frozen contracts (`backend/roadtwin/contracts.py`) are the only things
that cross module boundaries: **RoadNetwork**, **Scenario**, **SimulationRun**.

**Performance:** SUMO's native outputs (`tripinfo`, `summary`, `edgeData`)
produce all aggregate metrics; TraCI is used only for dynamic events, signal
control and playback capture — via *subscriptions*, so capturing every vehicle
costs one round-trip per step instead of three per vehicle per step. Measured
**~200× realtime** on a 380-segment network.

Simulations run in a `ProcessPoolExecutor`, not threads: TraCI keeps
per-process global connection state, so two simulations in one process would
fight over it. Separate processes also let a scenario sweep use every core.

## Quick start

```bash
./setup.sh     # venv + SUMO via pip (no brew tap, no compilation) + npm install
./run.sh       # API on :8099, dashboard on :3000
```

Requires Python 3.11+ and Node 18+. SUMO arrives as the `eclipse-sumo` wheel,
which ships `sumo`, `netconvert`, `duarouter` and the tools directory.

## Measured results (Koramangala, Bengaluru — 1,010 junctions, 2,546 segments)

Network generated in **~0.15 s** from cache; 158 km of road, 8 signalised
junctions reconstructed with full phase plans.

| Scenario (7 min horizon) | Speed km/h | Delay s | Served | Teleports |
|---|---|---|---|---|
| Baseline | 37.9 | 59.5 | 476 | 0 |
| Peak +40%, fixed-time signals | 35.9 | 67.6 | 538 | 9 |
| Peak +40%, adaptive | 37.5 | 61.9 | 552 | 6 |
| Peak +40%, **max-pressure** | **37.6** | **58.4** | **559** | 6 |
| Peak +40%, heavy rain | 28.8 | 77.0 | 444 | 1 |

Max-pressure control absorbs a 40% demand increase back to roughly baseline
delay while serving more vehicles. Six scenarios ran concurrently in 8.1 s
wall against 34.0 s serial — a **4.2× speedup**.

## Layout

```
backend/roadtwin/
  contracts.py        the three frozen types
  config.py           paths, SUMO discovery, city presets
  osm/                Overpass fetch → netconvert → RoadNetwork
  enrich/indian.py    effective capacity, encroachment, PCU
  sim/
    vtypes.py         calibrated six-class Indian fleet (sublane)
    demand.py         routing, fringe-biased, corridor-concentrated
    controllers.py    max-pressure and adaptive signal control
    runner.py         the engine
    analysis.py       bottleneck ranking + causal attribution
  ai/planner.py       natural language → validated Scenario
  api/                FastAPI + process-pool scheduler
frontend/             Next.js + MapLibre dashboard
```

## Licensing

SUMO is EPL-2.0 and is used as an unmodified upstream dependency via the
`eclipse-sumo` PyPI wheel; we invoke its binaries and TraCI API rather than
linking modified sources. OSM data is ODbL — derived networks carry that
obligation.
