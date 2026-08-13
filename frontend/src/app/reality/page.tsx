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

type Scene = { id: string; file: string; size_mb: number; url: string };

export default function RealityPage() {
  const [scenes, setScenes] = useState<Scene[]>([]);
  const [active, setActive] = useState<string | null>(null);

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
            Reality view — 80 Feet Road, Koramangala, reconstructed from street imagery
          </span>
        </div>
        <div className="flex items-center gap-3 text-[11px] text-white/45">
          {scenes[0] && <span>{scenes[0].size_mb} MB · 211,772 Gaussians</span>}
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
          <RealityViewer sceneId={active} />
        ) : (
          <div className="flex h-full items-center justify-center">
            <p className="text-sm text-white/50">
              No trained reconstruction found. Run the reality pipeline first.
            </p>
          </div>
        )}

        <div className="pointer-events-none absolute bottom-3 left-3 max-w-sm rounded-lg border border-white/10 bg-black/75 px-3 py-2 text-[11px] backdrop-blur">
          <div className="mb-0.5 text-[10px] uppercase tracking-wider text-sky-300/70">
            Provenance
          </div>
          <p className="leading-snug text-white/60">
            Mapillary street imagery → COLMAP structure-from-motion (44/45 frames
            registered) → 3D Gaussian Splatting trained on Apple Metal. This is the
            junction RoadTwin independently diagnoses as the worst bottleneck.
          </p>
        </div>
      </div>
    </main>
  );
}
