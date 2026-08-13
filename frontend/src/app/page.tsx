"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  api,
  CLASS_COLOURS,
  CLASS_LABELS,
  newScenario,
  waitForExperiment,
  waitForRun,
  type Bottleneck,
  type Comparison,
  type Experiment,
  type InterventionKey,
  type NetworkSummary,
  type PlaybackFrame,
  type Preset,
  type Scenario,
  type SignalStrategy,
  type SimulationRun,
  type Weather,
} from "@/lib/api";
import { Button, Panel, Select, Slider, Spinner, Stat } from "@/components/ui";

const MapView = dynamic(() => import("@/components/MapView"), { ssr: false });

type Geometry = {
  roads: GeoJSON.FeatureCollection;
  junctions: GeoJSON.FeatureCollection;
};

export default function Page() {
  const [presets, setPresets] = useState<Preset[]>([]);
  const [presetKey, setPresetKey] = useState("koramangala");
  const [network, setNetwork] = useState<NetworkSummary | null>(null);
  const [geometry, setGeometry] = useState<Geometry | null>(null);
  const [building, setBuilding] = useState(false);
  const [buildMs, setBuildMs] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [workers, setWorkers] = useState(0);

  const [scenario, setScenario] = useState<Scenario>(() =>
    newScenario({ name: "Custom scenario" }),
  );
  const [prompt, setPrompt] = useState(
    "Close one lane of 80 Feet Road during peak traffic and simulate for 10 minutes",
  );
  const [explanation, setExplanation] = useState<string[]>([]);
  const [parsing, setParsing] = useState(false);

  const [baseline, setBaseline] = useState<SimulationRun | null>(null);
  const [current, setCurrent] = useState<SimulationRun | null>(null);
  const [comparison, setComparison] = useState<Comparison | null>(null);
  const [running, setRunning] = useState<string | null>(null);
  const [experiment, setExperiment] = useState<Experiment | null>(null);
  const [experimenting, setExperimenting] = useState(false);
  const [interventions, setInterventions] = useState<InterventionKey[]>([
    "add_lane",
    "adaptive",
    "max_pressure",
  ]);

  const [focusKey, setFocusKey] = useState<string | null>(null);
  const [frames, setFrames] = useState<PlaybackFrame[]>([]);
  const [frameIndex, setFrameIndex] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [showBasemap, setShowBasemap] = useState(false);
  const [threeD, setThreeD] = useState(false);
  const [cinematic, setCinematic] = useState(false);
  const [buildings, setBuildings] = useState<GeoJSON.FeatureCollection | null>(null);
  const [buildingInfo, setBuildingInfo] = useState<Record<string, unknown> | null>(null);

  useEffect(() => {
    api.presets().then(setPresets).catch(() => setError("Backend unreachable."));
    api
      .health()
      .then((h) => setWorkers(h.workers))
      .catch(() => setError("Backend unreachable — start the API on port 8099."));
  }, []);

  // ------------------------------------------------------------ build twin
  const buildTwin = useCallback(async () => {
    setBuilding(true);
    setError(null);
    setBaseline(null);
    setCurrent(null);
    setComparison(null);
    setFrames([]);
    setExperiment(null);
    const started = performance.now();
    try {
      const { network: built } = await api.buildNetwork(presetKey);
      setNetwork(built);
      const geo = await api.geometry(built.id);
      setGeometry(geo);
      // Physical layer loads in the background; the twin is usable without it.
      api
        .buildings(built.id)
        .then((fc) => {
          setBuildings(fc);
          setBuildingInfo((fc.properties ?? null) as Record<string, unknown> | null);
        })
        .catch(() => setBuildings(null));
      setBuildMs(performance.now() - started);
    } catch (exc) {
      setError(String(exc));
    } finally {
      setBuilding(false);
    }
  }, [presetKey]);

  // -------------------------------------------------------------- run sim
  const execute = useCallback(
    async (target: Scenario, as: "baseline" | "scenario") => {
      if (!network) return;
      setRunning(as);
      setError(null);
      try {
        const queued = await api.run(network.id, target);
        const finished = await waitForRun(queued.id);
        if (finished.status === "failed") {
          setError(finished.error ?? "Simulation failed");
          return;
        }
        if (as === "baseline") {
          setBaseline(finished);
          setCurrent(finished);
          setComparison(null);
        } else {
          setCurrent(finished);
          if (baseline) {
            setComparison(await api.compare(baseline.id, finished.id));
          }
        }
        try {
          const { frames: loaded } = await api.playback(finished.id);
          setFrames(loaded);
          setFrameIndex(0);
          setPlaying(true);
        } catch {
          setFrames([]);
        }
      } catch (exc) {
        setError(String(exc));
      } finally {
        setRunning(null);
      }
    },
    [network, baseline],
  );

  const runBaseline = useCallback(
    () =>
      execute(
        newScenario({ name: "Baseline", duration_s: scenario.duration_s }),
        "baseline",
      ),
    [execute, scenario.duration_s],
  );

  // ------------------------------------------------------ intervention lab
  const runExperiment = useCallback(async () => {
    if (!network || interventions.length === 0) return;
    setExperimenting(true);
    setError(null);
    try {
      // Interventions are aimed at the bottlenecks the baseline actually found,
      // not at whichever road is structurally biggest. Widening a road that is
      // not the constraint is exactly the mistake this tool exists to prevent.
      const targets = (baseline?.bottlenecks ?? []).slice(0, 3).map((b) => b.segment_id);
      const started = await api.experiment(
        network.id,
        { ...scenario, lane_closures: [], lane_additions: [], incidents: [] },
        interventions,
        targets,
      );
      const finished = await waitForExperiment(started.id, setExperiment);
      setExperiment(finished);
    } catch (exc) {
      setError(String(exc));
    } finally {
      setExperimenting(false);
    }
  }, [network, scenario, interventions, baseline]);

  const toggleIntervention = (key: InterventionKey) =>
    setInterventions((prev) =>
      prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key],
    );

  // ------------------------------------------------------------- NL parse
  const parsePrompt = useCallback(async () => {
    if (!network) return;
    setParsing(true);
    setError(null);
    try {
      const { scenario: parsed, explanation: notes } = await api.parseScenario(
        network.id,
        prompt,
      );
      setScenario(parsed);
      setExplanation(notes);
    } catch (exc) {
      setError(String(exc));
    } finally {
      setParsing(false);
    }
  }, [network, prompt]);

  const patch = (values: Partial<Scenario>) =>
    setScenario((previous) => ({ ...previous, ...values }));

  const stats = network?.stats;
  const enrichment = network?.enrichment;
  const metrics = current?.metrics;
  const pct = comparison?.deltas_pct;

  const chartData = useMemo(
    () =>
      (current?.timeseries ?? []).map((point) => ({
        t: Math.round(point.t),
        speed: point.mean_speed_kmh,
        running: point.running,
        halting: point.halting,
      })),
    [current],
  );

  const frameVehicles = frames[frameIndex]?.v.length ?? 0;

  /** The segments the baseline diagnosed as bottlenecks -- what we intervene on. */
  const diagnosedSegments = useMemo(
    () => (baseline?.bottlenecks ?? []).slice(0, 3).map((b) => b.segment_id),
    [baseline],
  );

  // Selecting an intervention shows WHERE it acts: the widened/closed roads for
  // a geometry change, the signalised junctions for a control change.
  const highlightSegments = useMemo(() => {
    if (focusKey === "add_lane" || focusKey === "close_lane") return diagnosedSegments;
    return [];
  }, [focusKey, diagnosedSegments]);

  const highlightSignals = useMemo(() => {
    if (focusKey === "adaptive" || focusKey === "max_pressure") {
      return (network?.signals ?? []).map((s) => ({ lat: s.lat, lon: s.lon }));
    }
    return [];
  }, [focusKey, network]);

  const worst = baseline?.bottlenecks?.[0] ?? null;

  /** Camera path along the diagnosed corridor, taken from its real geometry. */
  const flightPath = useMemo<[number, number][]>(() => {
    if (!geometry) return [];
    const wanted = new Set(diagnosedSegments);
    const pts: [number, number][] = [];
    for (const f of geometry.roads.features) {
      if (!wanted.has(f.properties?.id as string)) continue;
      if (f.geometry.type !== "LineString") continue;
      for (const c of f.geometry.coordinates) pts.push([c[0], c[1]] as [number, number]);
    }
    if (pts.length > 1) return pts;
    // Before a baseline exists, fly the longest arterial so the mode still works.
    const arterial = geometry.roads.features
      .filter((f) => f.geometry.type === "LineString" && f.properties?.lanes >= 2)
      .sort((a, b) => (b.properties?.length_m ?? 0) - (a.properties?.length_m ?? 0))[0];
    return arterial && arterial.geometry.type === "LineString"
      ? (arterial.geometry.coordinates.map((c) => [c[0], c[1]]) as [number, number][])
      : [];
  }, [geometry, diagnosedSegments]);
  const ranked = useMemo(
    () =>
      (experiment?.results ?? [])
        .filter((r) => !r.is_control && !r.failed)
        .slice()
        .sort((a, b) => a.metrics.avg_delay_s - b.metrics.avg_delay_s),
    [experiment],
  );
  const control = experiment?.results.find((r) => r.is_control) ?? null;

  return (
    <main className="flex h-screen w-screen flex-col overflow-hidden bg-[#05070c] text-white">
      {/* ------------------------------------------------------------ header */}
      <header className="flex shrink-0 items-center justify-between border-b border-white/10 px-4 py-2.5">
        <div className="flex items-baseline gap-3">
          <h1 className="text-sm font-semibold tracking-tight">
            Road<span className="text-sky-400">Twin</span>
          </h1>
          <span className="hidden text-[11px] text-white/40 sm:inline">
            Automated high-fidelity road network modelling for Indian traffic simulation
          </span>
        </div>
        <div className="flex items-center gap-3 text-[11px] text-white/45">
          {workers > 0 && (
            <span className="hidden md:inline">
              {workers} simulation workers · SUMO 1.27
            </span>
          )}
          {network && (
            <button
              onClick={() => {
                const next = !cinematic;
                setCinematic(next);
                if (next) setThreeD(true);
              }}
              className={`rounded-md px-2.5 py-1 text-[11px] font-medium ${
                cinematic
                  ? "bg-sky-500 text-white"
                  : "border border-sky-400/40 bg-sky-400/10 text-sky-200 hover:bg-sky-400/20"
              }`}
            >
              {cinematic ? "◼ Exit cinematic" : "▶ Cinematic view"}
            </button>
          )}
          {network && (
            <Link
              href="/reality"
              className="rounded-md border border-sky-400/40 bg-sky-400/10 px-2.5 py-1 text-[11px] font-medium text-sky-200 hover:bg-sky-400/20"
            >
              View reality →
            </Link>
          )}
          <label className="flex cursor-pointer items-center gap-1.5">
            <input
              type="checkbox"
              checked={threeD}
              onChange={(event) => setThreeD(event.target.checked)}
              disabled={!buildings}
              className="accent-sky-400"
            />
            3D city
            {buildingInfo ? (
              <span className="text-white/30">
                ({(buildingInfo.count as number)?.toLocaleString()})
              </span>
            ) : null}
          </label>
          <label className="flex cursor-pointer items-center gap-1.5">
            <input
              type="checkbox"
              checked={showBasemap}
              onChange={(event) => setShowBasemap(event.target.checked)}
              className="accent-sky-400"
            />
            Basemap
          </label>
        </div>
      </header>

      {error && (
        <div className="shrink-0 border-b border-rose-500/30 bg-rose-500/10 px-4 py-1.5 text-[11px] text-rose-200">
          {error}
        </div>
      )}

      <div className="flex min-h-0 flex-1">
        {/* -------------------------------------------------------- left rail */}
        <aside className="w-[300px] shrink-0 space-y-3 overflow-y-auto border-r border-white/10 p-3">
          <Panel title="1 · Study area" subtitle="OpenStreetMap extract">
            <Select
              label="Location"
              value={presetKey}
              onChange={setPresetKey}
              options={presets.map((preset) => ({
                value: preset.key,
                label: preset.name,
              }))}
            />
            <p className="mt-1.5 text-[10px] leading-snug text-white/35">
              {presets.find((p) => p.key === presetKey)?.description}
            </p>
            <Button
              onClick={buildTwin}
              disabled={building}
              className="mt-2.5 w-full"
            >
              {building ? "Building…" : "Generate digital twin"}
            </Button>
          </Panel>

          {stats && (
            <Panel
              title="2 · Generated network"
              subtitle={`Built in ${((buildMs ?? 0) / 1000).toFixed(1)}s`}
            >
              <div className="grid grid-cols-2 gap-1.5">
                <Stat label="Junctions" value={stats.junctions} />
                <Stat label="Segments" value={stats.segments} />
                <Stat label="Signals" value={stats.signalised_junctions} />
                <Stat label="Lane-km" value={stats.lane_km.toFixed(1)} />
              </div>
              <div className="mt-2 rounded-lg border border-emerald-400/20 bg-emerald-400/[0.06] px-3 py-2">
                <div className="text-[10px] uppercase tracking-wider text-emerald-300/70">
                  Manual modeling effort avoided
                </div>
                <div className="mt-0.5 text-lg font-semibold text-emerald-300">
                  ~{stats.manual_effort_hours_estimate.toFixed(0)} hours →{" "}
                  {((buildMs ?? 0) / 1000).toFixed(1)} sec
                </div>
                <p className="mt-1 text-[10px] leading-snug text-white/35">
                  Estimated manual build at 0.5 h/junction + 0.2 h/km of coded link
                  geometry.
                </p>
              </div>
            </Panel>
          )}

          {enrichment && (
            <Panel
              title="3 · Indian road semantics"
              subtitle="Estimated capacity and heterogeneous-road behaviour"
            >
              <div className="grid grid-cols-2 gap-1.5">
                <Stat
                  label="Capacity lost"
                  value={enrichment.capacity_lost_to_encroachment_pct.toFixed(1)}
                  unit="%"
                />
                <Stat
                  label="Mean encroach."
                  value={(enrichment.mean_encroachment * 100).toFixed(0)}
                  unit="%"
                />
                <Stat label="Poor surface" value={enrichment.poor_surface_segments} />
                <Stat
                  label="Eff. lanes"
                  value={enrichment.mean_effective_lanes.toFixed(2)}
                />
              </div>
              <p className="mt-2 text-[10px] leading-snug text-white/35">
                Nominal lane counts overstate usable capacity: kerbside parking,
                vendors and encroachment remove roughly a third of it.
              </p>
            </Panel>
          )}
        </aside>

        {/* ------------------------------------------------------------- map */}
        <div className="relative min-w-0 flex-1">
          <MapView
            geometry={geometry}
            center={network?.center ?? null}
            segmentMetrics={current?.segment_metrics ?? []}
            bottlenecks={current?.bottlenecks ?? []}
            frames={frames}
            playing={playing}
            frameIndex={frameIndex}
            onFrameChange={setFrameIndex}
            showBasemap={showBasemap}
            highlightSegments={highlightSegments}
            highlightSignals={highlightSignals}
            buildings={buildings}
            threeD={threeD}
            cinematic={cinematic}
            flightPath={flightPath}
          />

          {!network && !building && (
            <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
              <div className="max-w-sm rounded-xl border border-white/10 bg-black/70 px-5 py-4 text-center backdrop-blur">
                <p className="text-sm text-white/80">
                  Select a study area and generate the digital twin.
                </p>
                <p className="mt-1 text-[11px] text-white/40">
                  OSM is converted into a simulation-ready network with junctions,
                  turn lanes and signal plans.
                </p>
              </div>
            </div>
          )}

          {building && (
            <div className="absolute inset-0 flex items-center justify-center bg-black/50">
              <Spinner label="Fetching OSM · reconstructing junctions · synthesising signals…" />
            </div>
          )}

          {/* playback transport */}
          {frames.length > 0 && (
            <div className="absolute bottom-3 left-1/2 flex w-[min(560px,90%)] -translate-x-1/2 items-center gap-3 rounded-xl border border-white/10 bg-black/80 px-3 py-2 backdrop-blur">
              <Button variant="ghost" onClick={() => setPlaying((p) => !p)}>
                {playing ? "❚❚" : "▶"}
              </Button>
              <input
                type="range"
                min={0}
                max={frames.length - 1}
                value={frameIndex}
                onChange={(event) => {
                  setPlaying(false);
                  setFrameIndex(Number(event.target.value));
                }}
                className="flex-1 accent-sky-400"
              />
              <span className="w-28 shrink-0 text-right text-[11px] tabular-nums text-white/50">
                t={Math.round(frames[frameIndex]?.t ?? 0)}s · {frameVehicles} veh
              </span>
            </div>
          )}

          {/* vehicle legend */}
          {frames.length > 0 && (
            // right-16 clears MapLibre's navigation control, which is pinned top-right.
            <div className="absolute right-16 top-3 rounded-lg border border-white/10 bg-black/75 px-3 py-2 backdrop-blur">
              <div className="mb-1 text-[10px] uppercase tracking-wider text-white/40">
                Fleet
              </div>
              <div className="space-y-0.5">
                {Object.entries(CLASS_LABELS).map(([code, label]) => (
                  <div key={code} className="flex items-center gap-1.5 text-[11px]">
                    <span
                      className="h-2 w-2 rounded-full"
                      style={{ background: CLASS_COLOURS[code] }}
                    />
                    <span className="text-white/60">{label}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* ------------------------------------------------------- right rail */}
        <aside className="w-[340px] shrink-0 space-y-3 overflow-y-auto border-l border-white/10 p-3">
          {/* Stage 2: once a baseline exists, the rail leads with what we
              learned about the road rather than with more controls. */}
          {worst && (
            <Panel
              title="Baseline diagnosis"
              subtitle="What the simulation found, before changing anything"
            >
              <div className="rounded-lg border border-rose-400/30 bg-rose-500/[0.07] px-3 py-2">
                <div className="text-[10px] uppercase tracking-wider text-rose-300/80">
                  Worst bottleneck
                </div>
                <div className="mt-0.5 truncate text-sm font-semibold text-white">
                  {worst.name}
                </div>
                <div className="mt-1.5 grid grid-cols-2 gap-2">
                  <div>
                    <div className="text-[10px] text-white/40">Speed</div>
                    <div className="text-base font-semibold tabular-nums text-rose-300">
                      {(worst.speed_ratio * 100).toFixed(0)}%
                      <span className="ml-1 text-[10px] font-normal text-white/40">
                        of free-flow
                      </span>
                    </div>
                  </div>
                  <div>
                    <div className="text-[10px] text-white/40">Queue</div>
                    <div className="text-base font-semibold tabular-nums text-rose-300">
                      {worst.queue_m.toFixed(0)}
                      <span className="ml-1 text-[10px] font-normal text-white/40">m</span>
                    </div>
                  </div>
                </div>
              </div>

              <div className="mt-2 text-[10px] uppercase tracking-wider text-white/40">
                Why
              </div>
              <div className="mt-1 space-y-1">
                {Object.entries(worst.causes).map(([cause, share]) => (
                  <div key={cause} className="flex items-center gap-2">
                    <span className="w-32 shrink-0 truncate text-[10px] text-white/60">
                      {cause}
                    </span>
                    <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-white/10">
                      <div
                        className="h-full rounded-full bg-rose-400/80"
                        style={{ width: `${share * 100}%` }}
                      />
                    </div>
                    <span className="w-8 shrink-0 text-right text-[10px] tabular-nums text-white/60">
                      {(share * 100).toFixed(0)}%
                    </span>
                  </div>
                ))}
              </div>

              {baseline?.explanation && (
                <p className="mt-2 rounded-md bg-white/[0.04] px-2.5 py-2 text-[11px] leading-snug text-white/80">
                  {baseline.explanation}
                </p>
              )}
            </Panel>
          )}

          <Panel
            title="Intervention lab"
            subtitle="What should we test?"
          >
            {/* The methodology is the differentiator, so it is stated on screen
                rather than left implicit in the numbers. */}
            <div className="mb-2 rounded-md border border-sky-400/25 bg-sky-400/[0.06] px-2.5 py-1.5">
              <div className="text-[10px] uppercase tracking-wider text-sky-300/80">
                Controlled experiment
              </div>
              <div className="mt-0.5 grid grid-cols-2 gap-x-2 text-[10px] text-white/60">
                <span>✓ Same demand</span>
                <span>✓ Same fleet mix</span>
                <span>✓ Same random seed</span>
                <span>✓ Same duration</span>
              </div>
              <div className="mt-0.5 text-[10px] text-sky-300/70">
                Only the intervention changes.
              </div>
            </div>
            <div className="space-y-1.5">
              {([
                ["add_lane", "Add one lane", "Widen the diagnosed bottleneck (network rebuilt)"],
                ["adaptive", "Adaptive signals", "Queue-responsive green times"],
                ["max_pressure", "Max-pressure signals", "Throughput-maximising control"],
                ["close_lane", "Close one lane", "Negative control / roadworks"],
              ] as [InterventionKey, string, string][]).map(([key, label, hint]) => (
                <label
                  key={key}
                  onMouseEnter={() => setFocusKey(key)}
                  onMouseLeave={() => setFocusKey(null)}
                  className="flex cursor-pointer items-start gap-2 rounded-md border border-white/10 bg-white/[0.02] px-2 py-1.5 hover:border-sky-400/40"
                >
                  <input
                    type="checkbox"
                    checked={interventions.includes(key)}
                    onChange={() => toggleIntervention(key)}
                    className="mt-0.5 accent-sky-400"
                  />
                  <span className="min-w-0">
                    <span className="block text-[11px] text-white/85">{label}</span>
                    <span className="block text-[10px] leading-snug text-white/35">{hint}</span>
                  </span>
                </label>
              ))}
              {!baseline && (
                <p className="text-[10px] leading-snug text-amber-300/70">
                  Run the baseline first — interventions are aimed at the bottlenecks it
                  finds, not at whichever road is largest.
                </p>
              )}
              <Button
                onClick={runExperiment}
                disabled={!network || experimenting || interventions.length === 0}
                className="w-full"
              >
                {experimenting
                  ? `Running ${interventions.length + 1} simulations in parallel…`
                  : `Compare ${interventions.length} interventions`}
              </Button>
            </div>
          </Panel>

          {experiment && ranked.length > 0 && (
            <Panel
              title="Intervention leaderboard"
              subtitle={`Ranked by average delay at ${(experiment.demand_multiplier * 100).toFixed(0)}% demand`}
            >
              <div className="space-y-1.5">
                {ranked.map((result, index) => {
                  const delay = result.deltas_pct?.avg_delay_s;
                  const completionPp = control
                    ? (result.metrics.completion_rate - control.metrics.completion_rate) * 100
                    : 0;
                  const medal = ["🏆", "🥈", "🥉"][index] ?? "  ";
                  return (
                    <button
                      key={result.key}
                      onMouseEnter={() => setFocusKey(result.key)}
                      onMouseLeave={() => setFocusKey(null)}
                      onClick={() =>
                        setFocusKey(focusKey === result.key ? null : result.key)
                      }
                      className={`flex w-full items-center gap-2 rounded-lg border px-2.5 py-2 text-left transition ${
                        index === 0
                          ? "border-emerald-400/40 bg-emerald-400/[0.08]"
                          : "border-white/10 bg-white/[0.02] hover:bg-white/[0.05]"
                      }`}
                    >
                      <span className="text-sm">{medal}</span>
                      <span className="min-w-0 flex-1 text-[11px] leading-tight text-white/90">
                        {result.label}
                      </span>
                      <span className="shrink-0 text-right">
                        <span
                          className={`block text-[12px] font-semibold tabular-nums ${
                            (delay ?? 0) < 0 ? "text-emerald-300" : "text-rose-300"
                          }`}
                        >
                          {delay === undefined
                            ? "—"
                            : `${delay > 0 ? "+" : ""}${delay.toFixed(1)}%`}
                        </span>
                        <span className="block text-[9px] tabular-nums text-white/40">
                          delay · {completionPp > 0 ? "+" : ""}
                          {completionPp.toFixed(1)} pp
                        </span>
                      </span>
                    </button>
                  );
                })}
              </div>

              {experiment.recommendation && (
                <div className="mt-2 rounded-lg border border-emerald-400/35 bg-emerald-400/[0.1] px-3 py-2.5">
                  <div className="text-[10px] uppercase tracking-wider text-emerald-300/80">
                    Recommended intervention
                  </div>
                  <p className="mt-1 text-[11px] leading-snug text-white/90">
                    {experiment.recommendation}
                  </p>
                </div>
              )}
              {experiment.diagnosis && (
                <p className="mt-1.5 text-[10px] leading-snug text-white/40">
                  {experiment.diagnosis}
                </p>
              )}
              <p className="mt-1.5 text-[10px] text-white/30">
                Hover a row to highlight where that intervention acts on the map.
              </p>
            </Panel>
          )}

          <Panel title="4 · Ask in plain English" subtitle="Parsed into a validated scenario">
            <textarea
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              rows={3}
              className="w-full resize-none rounded-md border border-white/10 bg-[#0d1119] px-2 py-1.5 text-xs text-white outline-none focus:border-sky-400/60"
              placeholder="e.g. close two lanes on Ejipura Main Road during peak hour and use adaptive signals"
            />
            <Button
              onClick={parsePrompt}
              disabled={!network || parsing}
              variant="ghost"
              className="mt-2 w-full"
            >
              {parsing ? "Interpreting…" : "Interpret → scenario"}
            </Button>
            {explanation.length > 0 && (
              <ul className="mt-2 space-y-1">
                {explanation.map((line) => (
                  <li key={line} className="flex gap-1.5 text-[11px] text-white/55">
                    <span className="text-sky-400">›</span>
                    {line}
                  </li>
                ))}
              </ul>
            )}
          </Panel>

          <Panel title="5 · Scenario" subtitle="Every field is validated before it reaches SUMO">
            <div className="space-y-2.5">
              <Slider
                label="Traffic demand"
                value={scenario.demand_multiplier}
                min={0.5}
                max={3}
                step={0.05}
                onChange={(value) => patch({ demand_multiplier: value })}
                format={(value) => `${(value * 100).toFixed(0)}%`}
              />
              <Slider
                label="Lane discipline"
                value={scenario.lane_discipline}
                min={0}
                max={1}
                step={0.05}
                onChange={(value) => patch({ lane_discipline: value })}
                format={(value) => value.toFixed(2)}
                hint="0 = heavy lateral filtering (sublane model), 1 = strict lane following"
              />
              <Slider
                label="Duration"
                value={scenario.duration_s}
                min={180}
                max={1800}
                step={60}
                onChange={(value) => patch({ duration_s: value })}
                format={(value) => `${Math.round(value / 60)} min`}
              />
              <div className="grid grid-cols-2 gap-2">
                <Select<Weather>
                  label="Weather"
                  value={scenario.weather}
                  onChange={(value) => patch({ weather: value })}
                  options={[
                    { value: "clear", label: "Clear" },
                    { value: "rain", label: "Rain" },
                    { value: "heavy_rain", label: "Heavy rain" },
                  ]}
                />
                <Select<SignalStrategy>
                  label="Signals"
                  value={scenario.signal_strategy}
                  onChange={(value) => patch({ signal_strategy: value })}
                  options={[
                    { value: "fixed", label: "Fixed-time" },
                    { value: "adaptive", label: "Adaptive" },
                    { value: "max_pressure", label: "Max-pressure" },
                  ]}
                />
              </div>

              {(scenario.lane_closures.length > 0 ||
                scenario.incidents.length > 0 ||
                scenario.obstructions.length > 0) && (
                <div className="rounded-md border border-amber-400/20 bg-amber-400/[0.06] px-2.5 py-1.5 text-[11px] text-amber-200/80">
                  {scenario.lane_closures.length > 0 &&
                    `${scenario.lane_closures.length} lane closure(s). `}
                  {scenario.incidents.length > 0 &&
                    `${scenario.incidents.length} incident(s). `}
                  {scenario.obstructions.length > 0 &&
                    `${scenario.obstructions.length} obstruction(s).`}
                  <button
                    onClick={() =>
                      patch({ lane_closures: [], incidents: [], obstructions: [] })
                    }
                    className="ml-1 underline decoration-dotted"
                  >
                    clear
                  </button>
                </div>
              )}

              <div className="flex gap-2">
                <Button
                  variant="ghost"
                  onClick={runBaseline}
                  disabled={!network || running !== null}
                  className="flex-1"
                >
                  {running === "baseline" ? "Running…" : "Run baseline"}
                </Button>
                <Button
                  onClick={() => execute(scenario, "scenario")}
                  disabled={!network || running !== null}
                  className="flex-1"
                >
                  {running === "scenario" ? "Running…" : "Run scenario"}
                </Button>
              </div>
            </div>
          </Panel>

          {metrics && (
            <Panel
              title="6 · Results"
              subtitle={
                current
                  ? `${current.sim_seconds.toFixed(0)}s simulated in ${current.wall_seconds.toFixed(1)}s (${current.realtime_factor.toFixed(0)}× realtime)`
                  : undefined
              }
            >
              <div className="grid grid-cols-2 gap-1.5">
                <Stat
                  label="Avg speed"
                  value={metrics.avg_speed_kmh.toFixed(1)}
                  unit="km/h"
                  delta={pct?.avg_speed_kmh}
                />
                <Stat
                  label="Avg delay"
                  value={metrics.avg_delay_s.toFixed(0)}
                  unit="s"
                  delta={pct?.avg_delay_s}
                  invert
                />
                <Stat
                  label="Travel time"
                  value={metrics.avg_travel_time_s.toFixed(0)}
                  unit="s"
                  delta={pct?.avg_travel_time_s}
                  invert
                />
                <Stat
                  label="Trip completion"
                  value={(metrics.completion_rate * 100).toFixed(1)}
                  unit="%"
                  delta={
                    pct?.completion_rate === undefined
                      ? undefined
                      : pct.completion_rate
                  }
                />
                <Stat
                  label="Max queue"
                  value={metrics.max_queue_m.toFixed(0)}
                  unit="m"
                  delta={pct?.max_queue_m}
                  invert
                />
                <Stat
                  label="Congestion"
                  value={(metrics.congestion_index * 100).toFixed(0)}
                  unit="%"
                />
              </div>
              {current?.explanation && (
                <div className="mt-2 rounded-md border border-white/10 bg-white/[0.04] px-2.5 py-2">
                  <div className="text-[10px] uppercase tracking-wider text-white/40">
                    Why this happened
                  </div>
                  <p className="mt-0.5 text-[11px] leading-snug text-white/80">
                    {current.explanation}
                  </p>
                </div>
              )}
              {comparison && (
                <p className="mt-1.5 rounded-md bg-white/[0.04] px-2.5 py-1.5 text-[11px] leading-snug text-white/70">
                  {comparison.verdict}
                </p>
              )}
              <p className="mt-1.5 text-[10px] text-white/30">
                Throughput {metrics.throughput_veh_hr.toFixed(0)} veh/h ·{" "}
                {metrics.vehicles_arrived}/{metrics.vehicles_loaded} trips completed ·
                CO₂ {metrics.total_co2_kg.toFixed(1)} kg
              </p>
              {metrics.teleports > 0 && (
                <p className="mt-1.5 text-[10px] text-amber-300/70">
                  {metrics.teleports} vehicles teleported — SUMO&apos;s gridlock escape
                  hatch, reported rather than hidden.
                </p>
              )}
            </Panel>
          )}

          {chartData.length > 1 && (
            <Panel title="Network speed over time">
              <ResponsiveContainer width="100%" height={130}>
                <AreaChart data={chartData} margin={{ top: 4, right: 4, bottom: 0, left: -26 }}>
                  <defs>
                    <linearGradient id="speedFill" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="#38bdf8" stopOpacity={0.5} />
                      <stop offset="100%" stopColor="#38bdf8" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid stroke="#ffffff10" vertical={false} />
                  <XAxis
                    dataKey="t"
                    tick={{ fill: "#ffffff55", fontSize: 10 }}
                    tickLine={false}
                    axisLine={false}
                  />
                  <YAxis
                    tick={{ fill: "#ffffff55", fontSize: 10 }}
                    tickLine={false}
                    axisLine={false}
                  />
                  <Tooltip
                    contentStyle={{
                      background: "#0b0f18",
                      border: "1px solid #ffffff20",
                      borderRadius: 8,
                      fontSize: 11,
                    }}
                  />
                  <Area
                    type="monotone"
                    dataKey="speed"
                    stroke="#38bdf8"
                    fill="url(#speedFill)"
                    strokeWidth={1.6}
                    name="km/h"
                  />
                  <Line
                    type="monotone"
                    dataKey="halting"
                    stroke="#f43f5e"
                    dot={false}
                    strokeWidth={1.2}
                    name="halting"
                  />
                </AreaChart>
              </ResponsiveContainer>
            </Panel>
          )}

          {current && current.bottlenecks.length > 0 && (
            <BottleneckPanel bottlenecks={current.bottlenecks} />
          )}
        </aside>
      </div>
    </main>
  );
}

function BottleneckPanel({ bottlenecks }: { bottlenecks: Bottleneck[] }) {
  const [open, setOpen] = useState<number | null>(0);
  return (
    <Panel title="7 · Bottlenecks" subtitle="Ranked, with attributed causes">
      <div className="space-y-1.5">
        {bottlenecks.slice(0, 5).map((bottleneck) => (
          <div
            key={`${bottleneck.rank}-${bottleneck.segment_id}`}
            className="rounded-lg border border-white/10 bg-white/[0.02]"
          >
            <button
              onClick={() => setOpen(open === bottleneck.rank ? null : bottleneck.rank)}
              className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left"
            >
              <span className="flex h-4 w-4 shrink-0 items-center justify-center rounded bg-rose-500/20 text-[10px] font-semibold text-rose-300">
                {bottleneck.rank}
              </span>
              <span className="min-w-0 flex-1 truncate text-[11px] text-white/85">
                {bottleneck.name}
              </span>
              <span className="shrink-0 text-[10px] tabular-nums text-rose-300">
                {(bottleneck.speed_ratio * 100).toFixed(0)}% free-flow
              </span>
            </button>
            {open === bottleneck.rank && (
              <div className="space-y-1 border-t border-white/10 px-2.5 py-2">
                {Object.entries(bottleneck.causes).map(([cause, share]) => (
                  <div key={cause} className="flex items-center gap-2">
                    <span className="w-36 shrink-0 truncate text-[10px] text-white/55">
                      {cause}
                    </span>
                    <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-white/10">
                      <div
                        className="h-full rounded-full bg-rose-400/80"
                        style={{ width: `${share * 100}%` }}
                      />
                    </div>
                    <span className="w-8 shrink-0 text-right text-[10px] tabular-nums text-white/60">
                      {(share * 100).toFixed(0)}%
                    </span>
                  </div>
                ))}
                <div className="pt-1 text-[10px] text-white/35">
                  Queue {bottleneck.queue_m.toFixed(0)} m · waiting{" "}
                  {bottleneck.mean_waiting_s.toFixed(1)} s
                </div>
              </div>
            )}
          </div>
        ))}
      </div>
    </Panel>
  );
}
