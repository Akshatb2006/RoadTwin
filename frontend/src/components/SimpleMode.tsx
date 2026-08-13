"use client";

import dynamic from "next/dynamic";
import { useCallback, useEffect, useState } from "react";
import {
  api,
  newScenario,
  waitForExperiment,
  waitForRun,
  type Experiment,
  type NetworkSummary,
  type Preset,
  type SimulationRun,
} from "@/lib/api";

const MapView = dynamic(() => import("@/components/MapView"), { ssr: false });

/**
 * The view for someone who wants an answer, not a dashboard.
 *
 * The full interface exposes sublane parameters, PCU capacity, congestion
 * indices and teleport counts. That is the right depth for an engineer and the
 * wrong thing entirely for the official who has to make the decision -- they
 * will read a wall of jargon and hand the file to somebody else.
 *
 * So this asks three questions in order, one screen each: where, what is wrong,
 * what should we do. Every number is converted into a sentence, and anything
 * requiring domain vocabulary is simply not shown here.
 */

type Step = "place" | "problem" | "fix";

const FIXES = [
  {
    key: "add_lane" as const,
    label: "Widen the road",
    detail: "Add one lane where traffic backs up",
  },
  {
    key: "adaptive" as const,
    label: "Smarter signals",
    detail: "Green lights react to how many vehicles are waiting",
  },
  {
    key: "max_pressure" as const,
    label: "Best-effort signals",
    detail: "Signals continuously favour the busiest direction",
  },
];

/** Plain-language description of how bad a bottleneck is. */
function severityWords(ratio: number): string {
  if (ratio < 0.15) return "almost at a standstill";
  if (ratio < 0.35) return "crawling";
  if (ratio < 0.6) return "much slower than it should be";
  return "slower than normal";
}

export default function SimpleMode({ onAdvanced }: { onAdvanced: () => void }) {
  const [presets, setPresets] = useState<Preset[]>([]);
  const [presetKey, setPresetKey] = useState("koramangala");
  const [network, setNetwork] = useState<NetworkSummary | null>(null);
  const [geometry, setGeometry] = useState<{
    roads: GeoJSON.FeatureCollection;
    junctions: GeoJSON.FeatureCollection;
  } | null>(null);
  const [baseline, setBaseline] = useState<SimulationRun | null>(null);
  const [experiment, setExperiment] = useState<Experiment | null>(null);
  const [step, setStep] = useState<Step>("place");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.presets().then(setPresets).catch(() => setError("Cannot reach the server."));
  }, []);

  // One button does the whole first stage: build the model and study today's
  // traffic. Splitting it would expose a distinction the user does not care about.
  const analyse = useCallback(async () => {
    setBusy("Building a model of these roads…");
    setError(null);
    try {
      const { network: built } = await api.buildNetwork(presetKey);
      setNetwork(built);
      setGeometry(await api.geometry(built.id));
      setBusy("Studying how traffic behaves today…");
      const queued = await api.run(
        built.id,
        newScenario({ name: "Today", duration_s: 600 }),
      );
      const finished = await waitForRun(queued.id);
      setBaseline(finished);
      setStep("problem");
    } catch (exc) {
      setError(String(exc).slice(0, 200));
    } finally {
      setBusy(null);
    }
  }, [presetKey]);

  const testFixes = useCallback(async () => {
    if (!network) return;
    setBusy("Trying each option on the same traffic…");
    setError(null);
    try {
      const targets = (baseline?.bottlenecks ?? []).slice(0, 3).map((b) => b.segment_id);
      const started = await api.experiment(
        network.id,
        newScenario({ demand_multiplier: 1.2, duration_s: 600 }),
        FIXES.map((f) => f.key),
        targets,
      );
      setExperiment(await waitForExperiment(started.id));
      setStep("fix");
    } catch (exc) {
      setError(String(exc).slice(0, 200));
    } finally {
      setBusy(null);
    }
  }, [network, baseline]);

  const worst = baseline?.bottlenecks?.[0] ?? null;
  const topCause = worst ? Object.entries(worst.causes)[0] : null;

  const ranked = (experiment?.results ?? [])
    .filter((r) => !r.is_control && !r.failed)
    .slice()
    .sort((a, b) => a.metrics.avg_delay_s - b.metrics.avg_delay_s);
  const control = experiment?.results.find((r) => r.is_control) ?? null;
  const best = ranked[0];

  return (
    <main className="relative h-screen w-screen overflow-hidden bg-[#05070c] text-white">
      <div className="absolute inset-0">
        <MapView
          geometry={geometry}
          center={network?.center ?? null}
          segmentMetrics={baseline?.segment_metrics ?? []}
          bottlenecks={baseline?.bottlenecks ?? []}
          frames={[]}
          playing={false}
          frameIndex={0}
          onFrameChange={() => {}}
          showBasemap={false}
        />
      </div>
      <div className="pointer-events-none absolute inset-0 bg-gradient-to-r from-black/85 via-black/30 to-transparent" />

      <header className="absolute left-0 right-0 top-0 flex items-center justify-between px-6 py-4">
        <span className="text-base font-semibold">
          Road<span className="text-sky-400">Twin</span>
        </span>
        <button
          onClick={onAdvanced}
          className="rounded-md border border-white/15 px-3 py-1.5 text-xs text-white/60 hover:bg-white/5"
        >
          Detailed view
        </button>
      </header>

      <div className="absolute inset-y-0 left-0 flex w-[min(560px,90%)] items-center px-6">
        <div className="w-full">
          {error && (
            <div className="mb-3 rounded-lg border border-rose-500/40 bg-rose-500/10 px-4 py-2 text-sm text-rose-200">
              {error}
            </div>
          )}

          {/* ---------------- 1. Where ---------------- */}
          {step === "place" && (
            <div>
              <h1 className="text-3xl font-semibold leading-tight">
                Which area do you want to look at?
              </h1>
              <p className="mt-2 text-sm text-white/50">
                We will build a working model of its roads and study the traffic.
              </p>
              <select
                value={presetKey}
                onChange={(e) => setPresetKey(e.target.value)}
                className="mt-5 w-full rounded-lg border border-white/15 bg-[#0d1119] px-4 py-3 text-base text-white outline-none focus:border-sky-400"
              >
                {presets.map((p) => (
                  <option key={p.key} value={p.key}>
                    {p.name}
                  </option>
                ))}
              </select>
              <button
                onClick={analyse}
                disabled={!!busy}
                className="mt-4 w-full rounded-lg bg-sky-500 px-5 py-3.5 text-base font-semibold text-white hover:bg-sky-400 disabled:bg-sky-500/40"
              >
                {busy ?? "Check this area"}
              </button>
              {busy && (
                <p className="mt-3 text-center text-xs text-white/40">
                  This takes about half a minute.
                </p>
              )}
            </div>
          )}

          {/* ---------------- 2. What is wrong ---------------- */}
          {step === "problem" && worst && (
            <div>
              <p className="text-xs uppercase tracking-[0.2em] text-rose-300/80">
                Worst traffic problem
              </p>
              <h1 className="mt-1 text-3xl font-semibold leading-tight">{worst.name}</h1>
              <p className="mt-3 text-lg leading-relaxed text-white/85">
                Traffic here is {severityWords(worst.speed_ratio)} — moving at about{" "}
                <strong className="text-rose-300">
                  {Math.round(worst.speed_ratio * 100)}%
                </strong>{" "}
                of the speed this road is built for, with roughly{" "}
                <strong className="text-rose-300">{Math.round(worst.queue_m)} m</strong> of
                vehicles queued.
              </p>
              {topCause && (
                <p className="mt-3 text-lg leading-relaxed text-white/85">
                  The biggest single cause is{" "}
                  <strong className="text-white">{topCause[0].toLowerCase()}</strong>,
                  responsible for about{" "}
                  <strong className="text-white">
                    {Math.round(topCause[1] * 100)}%
                  </strong>{" "}
                  of the problem.
                </p>
              )}
              <button
                onClick={testFixes}
                disabled={!!busy}
                className="mt-6 w-full rounded-lg bg-sky-500 px-5 py-3.5 text-base font-semibold text-white hover:bg-sky-400 disabled:bg-sky-500/40"
              >
                {busy ?? "What can we do about it?"}
              </button>
              <p className="mt-3 text-xs text-white/35">
                We will test three options on exactly the same traffic, so the
                comparison is fair.
              </p>
            </div>
          )}

          {/* ---------------- 3. What to do ---------------- */}
          {step === "fix" && best && control && (
            <div>
              <p className="text-xs uppercase tracking-[0.2em] text-emerald-300/80">
                Recommended
              </p>
              <h1 className="mt-1 text-3xl font-semibold leading-tight">
                {FIXES.find((f) => f.key === best.key)?.label ?? best.label}
              </h1>
              <p className="mt-3 text-lg leading-relaxed text-white/85">
                This saves about{" "}
                <strong className="text-emerald-300">
                  {Math.max(
                    0,
                    Math.round(control.metrics.avg_delay_s - best.metrics.avg_delay_s),
                  )}{" "}
                  seconds
                </strong>{" "}
                of waiting for every vehicle, and gets{" "}
                <strong className="text-emerald-300">
                  {Math.max(
                    0,
                    best.metrics.vehicles_arrived - control.metrics.vehicles_arrived,
                  )}
                </strong>{" "}
                more vehicles through.
              </p>

              <div className="mt-5 space-y-2">
                {ranked.map((r, i) => {
                  const fix = FIXES.find((f) => f.key === r.key);
                  const saved = Math.round(
                    control.metrics.avg_delay_s - r.metrics.avg_delay_s,
                  );
                  return (
                    <div
                      key={r.key}
                      className={`flex items-center gap-3 rounded-lg border px-4 py-3 ${
                        i === 0
                          ? "border-emerald-400/40 bg-emerald-400/[0.08]"
                          : "border-white/10 bg-white/[0.03]"
                      }`}
                    >
                      <span className="min-w-0 flex-1">
                        <span className="block text-sm font-medium text-white">
                          {fix?.label ?? r.label}
                        </span>
                        <span className="block text-xs text-white/45">
                          {fix?.detail}
                        </span>
                      </span>
                      <span
                        className={`shrink-0 text-right text-sm font-semibold tabular-nums ${
                          saved > 0 ? "text-emerald-300" : "text-rose-300"
                        }`}
                      >
                        {saved > 0
                          ? `saves ${saved}s`
                          : `costs ${Math.abs(saved)}s`}
                      </span>
                    </div>
                  );
                })}
              </div>

              <button
                onClick={() => {
                  setStep("place");
                  setBaseline(null);
                  setExperiment(null);
                }}
                className="mt-5 rounded-md border border-white/15 px-4 py-2 text-sm text-white/70 hover:bg-white/5"
              >
                Check another area
              </button>
            </div>
          )}
        </div>
      </div>
    </main>
  );
}
