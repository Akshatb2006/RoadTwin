"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { useEffect, useState } from "react";
import { API_BASE } from "@/lib/api";

// Isolated route on purpose. The reconstruction pipeline is experimental and
// must not be able to affect the simulation view at "/".
const RealityViewer = dynamic(() => import("@/components/RealityViewer"), {
  ssr: false,
});
const StreetDriveThrough = dynamic(
  () => import("@/components/StreetDriveThrough"),
  { ssr: false },
);

type Scene = { id: string; file: string; size_mb: number; url: string };

export default function RealityPage() {
  const [scenes, setScenes] = useState<Scene[]>([]);
  const [active, setActive] = useState<string | null>(null);
  // Photographs by default. The Gaussian reconstruction is available behind a
  // toggle as evidence the pipeline works, but it is not the thing to show.
  const [mode, setMode] = useState<"photo" | "splat">("photo");

  useEffect(() => {
    fetch(`${API_BASE}/api/reality/scenes`)
      .then((r) => r.json())
      .then((list: Scene[]) => {
        setScenes(list);
        if (list.length) setActive(list[0].id);
      })
      .catch(() => setScenes([]));
  }, []);

  return (
    <main className="flex h-screen w-screen flex-col overflow-hidden bg-[#05070c] text-white">
      <header className="flex shrink-0 items-center justify-between border-b border-white/10 px-4 py-2.5">
        <div className="flex items-baseline gap-3">
          <Link href="/" className="text-sm font-semibold tracking-tight hover:opacity-80">
            Road<span className="text-sky-400">Twin</span>
          </Link>
          <span className="text-[11px] text-white/40">
            {mode === "photo"
              ? "Reality view — 80 Feet Road, Koramangala: the street RoadTwin diagnoses"
              : "Reality view — Gaussian reconstruction (research artifact)"}
          </span>
        </div>
        <div className="flex items-center gap-3 text-[11px] text-white/45">
          <span>
            {mode === "photo"
              ? "431 frames · Mapillary"
              : `${scenes[0]?.size_mb ?? 42} MB · 168,889 splats`}
          </span>
          <div className="flex overflow-hidden rounded-md border border-white/15">
            {(["photo", "splat"] as const).map((m) => (
              <button
                key={m}
                onClick={() => setMode(m)}
                className={`px-2.5 py-1 text-[11px] ${
                  mode === m ? "bg-sky-500 text-white" : "text-white/70 hover:bg-white/5"
                }`}
              >
                {m === "photo" ? "Street imagery" : "3D reconstruction"}
              </button>
            ))}
          </div>
          <Link
            href="/"
            className="rounded-md border border-white/15 px-2.5 py-1 text-white/80 hover:bg-white/5"
          >
            ← Simulation view
          </Link>
        </div>
      </header>

      <div className="relative min-h-0 flex-1">
        {active ? (
          mode === "photo" ? (
            <StreetDriveThrough sceneId="seq2016_dense" />
          ) : (
            <RealityViewer sceneId={active} />
          )
        ) : (
          <div className="flex h-full items-center justify-center">
            <p className="text-sm text-white/50">
              No trained reconstruction found. Run the reality pipeline first.
            </p>
          </div>
        )}

        <div className="pointer-events-none absolute left-3 top-3 w-64 rounded-lg border border-white/10 bg-black/80 px-3 py-2.5 text-[11px] backdrop-blur">
          <div className="text-[10px] uppercase tracking-wider text-sky-300/70">
            Reality view
          </div>
          <div className="mt-0.5 text-sm font-semibold text-white">80 Feet Road</div>
          <div className="text-[10px] text-white/40">Koramangala, Bengaluru</div>

          <dl className="mt-2 space-y-1 border-t border-white/10 pt-2">
            {[
              ["Source", "Mapillary street imagery"],
              ["Corridor", "3.2 km, 431 frames"],
              ["Mode", mode === "photo" ? "Photographs, capture order" : "Gaussian splat"],
              ["Reconstruction", "COLMAP + 3DGS (Apple Metal)"],
              ["Registered frames", "44 / 45"],
            ].map(([k, v]) => (
              <div key={k} className="flex justify-between gap-2">
                <dt className="shrink-0 text-white/40">{k}</dt>
                <dd className="text-right text-white/75">{v}</dd>
              </div>
            ))}
          </dl>

          {/* Stating the limitation is the point. The reconstruction has no
              globally stable metric scale (Sim(3) fit against GPS: 74 m median
              over a 268 m corridor), so we do not overlay simulated vehicles
              and imply a precision we cannot support. */}
          <p className="mt-2 border-t border-white/10 pt-2 leading-snug text-white/45">
            {mode === "photo"
              ? "These are the actual photographs of the diagnosed bottleneck, played in capture order. Real imagery, no reconstruction — the road as it is."
              : "Reconstruction from forward-only imagery: well constrained along the driven path, weak away from it, and without globally stable scale (74 m median Sim(3) error). Shown as pipeline evidence, not as a metric frame."}
          </p>
        </div>
      </div>
    </main>
  );
}
