/**
 * Typed client for the RoadTwin API.
 * Mirrors backend/roadtwin/contracts.py -- keep the two in step.
 */

export const API_BASE =
  process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8099";

export interface Preset {
  key: string;
  name: string;
  description: string;
  bbox: { south: number; west: number; north: number; east: number };
  center: { lat: number; lon: number };
}

export interface NetworkStats {
  junctions: number;
  signalised_junctions: number;
  segments: number;
  lane_km: number;
  total_length_km: number;
  roundabouts: number;
  build_seconds: number;
  manual_effort_hours_estimate: number;
}

export interface Enrichment {
  segments_enriched: number;
  mean_encroachment: number;
  capacity_lost_to_encroachment_pct: number;
  mean_effective_lanes: number;
  total_capacity_pcu_hr: number;
  poor_surface_segments: number;
  road_class_mix: Record<string, number>;
}

export interface NetworkSummary {
  id: string;
  name: string;
  bbox: { south: number; west: number; north: number; east: number };
  center: { lat: number; lon: number };
  stats: NetworkStats;
  enrichment: Enrichment;
  signals: Array<{ id: string; lat: number; lon: number; cycle_time_s: number }>;
  segment_count: number;
}

export interface Metrics {
  avg_speed_kmh: number;
  avg_travel_time_s: number;
  avg_delay_s: number;
  max_queue_m: number;
  total_queue_m: number;
  throughput_veh_hr: number;
  vehicles_loaded: number;
  vehicles_arrived: number;
  vehicles_still_running: number;
  completion_rate: number;
  congestion_index: number;
  total_co2_kg: number;
  total_fuel_l: number;
  teleports: number;
}

export interface TimeseriesPoint {
  t: number;
  mean_speed_kmh: number;
  running: number;
  arrived_cumulative: number;
  halting: number;
  mean_waiting_s: number;
}

export interface SegmentMetric {
  segment_id: string;
  mean_speed_kmh: number;
  free_flow_speed_kmh: number;
  speed_ratio: number;
  density_veh_km: number;
  max_queue_m: number;
  mean_waiting_s: number;
  vehicles_seen: number;
}

export interface Bottleneck {
  rank: number;
  segment_id: string;
  junction_id?: string;
  name: string;
  lat: number;
  lon: number;
  severity: number;
  speed_ratio: number;
  mean_waiting_s: number;
  queue_m: number;
  causes: Record<string, number>;
}

export type SignalStrategy = "fixed" | "actuated" | "adaptive" | "max_pressure";
export type Weather = "clear" | "rain" | "heavy_rain";

export interface Scenario {
  id: string;
  name: string;
  description?: string;
  duration_s: number;
  seed: number;
  demand_multiplier: number;
  vehicle_mix: Record<string, number>;
  lane_closures: Array<{
    segment_id: string;
    lanes_closed: number;
    start_s: number;
    end_s: number;
  }>;
  lane_additions: Array<{ segment_id: string; lanes_added: number }>;
  incidents: Array<{ segment_id: string; position: number; duration_s: number }>;
  obstructions: Array<{ segment_id: string; kind: string; severity: number }>;
  signal_strategy: SignalStrategy;
  weather: Weather;
  lateral_resolution_m: number;
  lane_discipline: number;
  source_prompt?: string | null;
}

export interface SimulationRun {
  id: string;
  network_id: string;
  scenario: Scenario;
  status: "queued" | "building" | "running" | "done" | "failed";
  progress: number;
  error?: string | null;
  metrics: Metrics;
  timeseries: TimeseriesPoint[];
  segment_metrics: SegmentMetric[];
  bottlenecks: Bottleneck[];
  wall_seconds: number;
  sim_seconds: number;
  realtime_factor: number;
  widened_segments: Record<string, number[]>;
  explanation: string;
}

export type InterventionKey =
  | "add_lane"
  | "close_lane"
  | "adaptive"
  | "max_pressure";

export interface InterventionResult {
  key: string;
  label: string;
  run_id: string;
  metrics: Metrics;
  deltas_pct: Record<string, number>;
  is_control: boolean;
  failed: boolean;
}

export interface Experiment {
  id: string;
  network_id: string;
  demand_multiplier: number;
  duration_s: number;
  control_run_id: string;
  results: InterventionResult[];
  diagnosis: string;
  recommendation: string;
  best_key: string | null;
  finished?: boolean;
}

export interface Comparison {
  baseline_run_id: string;
  scenario_run_id: string;
  baseline: Metrics;
  scenario: Metrics;
  deltas: Record<string, number>;
  deltas_pct: Record<string, number>;
  verdict: string;
}

export type PlaybackFrame = {
  t: number;
  /** [lon, lat, angle, speedKmh, classCode] */
  v: [number, number, number, number, string][];
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    const body = await response.text();
    throw new Error(`${response.status} ${response.statusText}: ${body.slice(0, 400)}`);
  }
  return response.json() as Promise<T>;
}

export const api = {
  health: () => request<{ status: string; workers: number }>("/api/health"),

  presets: () => request<Preset[]>("/api/presets"),

  strategies: () =>
    request<Array<{ key: SignalStrategy; label: string; description: string }>>(
      "/api/strategies",
    ),

  buildNetwork: (preset: string, force = false) =>
    request<{ network: NetworkSummary; cached: boolean; build_seconds: number }>(
      "/api/networks",
      { method: "POST", body: JSON.stringify({ preset, force }) },
    ),

  geometry: (networkId: string) =>
    request<{
      roads: GeoJSON.FeatureCollection;
      junctions: GeoJSON.FeatureCollection;
    }>(`/api/networks/${networkId}/geometry`),

  parseScenario: (networkId: string, prompt: string) =>
    request<{ scenario: Scenario; explanation: string[] }>("/api/scenario/parse", {
      method: "POST",
      body: JSON.stringify({ network_id: networkId, prompt }),
    }),

  run: (networkId: string, scenario: Scenario) =>
    request<SimulationRun>("/api/runs", {
      method: "POST",
      body: JSON.stringify({ network_id: networkId, scenario }),
    }),

  sweep: (networkId: string, scenarios: Scenario[]) =>
    request<{ sweep_id: string; run_ids: string[]; workers: number }>("/api/sweep", {
      method: "POST",
      body: JSON.stringify({ network_id: networkId, scenarios }),
    }),

  getRun: (runId: string) => request<SimulationRun>(`/api/runs/${runId}`),

  playback: (runId: string) =>
    request<{ frames: PlaybackFrame[] }>(`/api/runs/${runId}/playback`),

  experiment: (
    networkId: string,
    baseScenario: Scenario,
    interventions: InterventionKey[],
    targetSegments: string[] = [],
  ) =>
    request<Experiment>("/api/experiment", {
      method: "POST",
      body: JSON.stringify({
        network_id: networkId,
        base_scenario: baseScenario,
        interventions,
        target_segments: targetSegments,
      }),
    }),

  getExperiment: (id: string) => request<Experiment>(`/api/experiment/${id}`),

  compare: (baselineRunId: string, scenarioRunId: string) =>
    request<Comparison>("/api/compare", {
      method: "POST",
      body: JSON.stringify({
        baseline_run_id: baselineRunId,
        scenario_run_id: scenarioRunId,
      }),
    }),
};

/** Poll an experiment until every run in it has settled. */
export async function waitForExperiment(
  id: string,
  onTick?: (experiment: Experiment) => void,
  intervalMs = 2000,
): Promise<Experiment> {
  for (;;) {
    const experiment = await api.getExperiment(id);
    onTick?.(experiment);
    if (experiment.finished) return experiment;
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
}

/** Poll a run until it reaches a terminal state. */
export async function waitForRun(
  runId: string,
  onTick?: (run: SimulationRun) => void,
  intervalMs = 1200,
): Promise<SimulationRun> {
  for (;;) {
    const run = await api.getRun(runId);
    onTick?.(run);
    if (run.status === "done" || run.status === "failed") return run;
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
}

export const DEFAULT_VEHICLE_MIX: Record<string, number> = {
  motorcycle: 0.42,
  car: 0.3,
  auto_rickshaw: 0.14,
  bus: 0.05,
  truck: 0.04,
  bicycle: 0.05,
};

export function newScenario(partial: Partial<Scenario> = {}): Scenario {
  return {
    id: `sc_${Math.random().toString(36).slice(2, 10)}`,
    name: "Scenario",
    duration_s: 600,
    seed: 42,
    demand_multiplier: 1,
    vehicle_mix: { ...DEFAULT_VEHICLE_MIX },
    lane_closures: [],
    lane_additions: [],
    incidents: [],
    obstructions: [],
    signal_strategy: "fixed",
    weather: "clear",
    lateral_resolution_m: 0.8,
    lane_discipline: 0.35,
    ...partial,
  };
}

/** Vehicle class code -> display colour, matching the SUMO vType colours. */
export const CLASS_COLOURS: Record<string, string> = {
  c: "#e6e8ee", // car
  m: "#ff7a1a", // motorcycle
  a: "#ffd91a", // auto rickshaw
  b: "#2a8cf0", // bus
  t: "#8c5a33", // truck
  y: "#66d966", // bicycle
};

export const CLASS_LABELS: Record<string, string> = {
  c: "Car",
  m: "Motorcycle",
  a: "Auto",
  b: "Bus",
  t: "Truck",
  y: "Bicycle",
};
