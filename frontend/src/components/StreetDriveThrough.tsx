"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import dynamic from "next/dynamic";
import { API_BASE } from "@/lib/api";

const AnimeFilter = dynamic(() => import("@/components/AnimeFilter"), { ssr: false });

type Frame = {
  file: string;
  url: string;
  lat: number | null;
  lon: number | null;
  compass: number | null;
};

/**
 * A drive-through built from the actual street photographs.
 *
 * The Gaussian reconstruction proved that the pipeline works, but forward-only
 * imagery cannot constrain a free-viewpoint scene, and no amount of pruning or
 * training fixes absent information. These are the same photographs the
 * reconstruction was built from, played in capture order -- so what a viewer
 * sees is unambiguously the real road, at full photographic fidelity.
 */
export default function StreetDriveThrough({
  sceneId,
  anime = false,
}: {
  sceneId: string;
  anime?: boolean;
}) {
  const [frames, setFrames] = useState<Frame[]>([]);
  const [index, setIndex] = useState(0);
  const [playing, setPlaying] = useState(true);
  const [ready, setReady] = useState(false);
  const [loaded, setLoaded] = useState(0);
  const timer = useRef<number | null>(null);

  useEffect(() => {
    fetch(`${API_BASE}/api/reality/${sceneId}/frames`)
      .then((r) => r.json())
      .then((data: { frames: Frame[] }) => {
        setFrames(data.frames);
        // Preload a lead-in so playback does not stutter on first pass.
        data.frames.slice(0, 40).forEach((f) => {
          const img = new Image();
          img.onload = () => setLoaded((n) => n + 1);
          img.src = `${API_BASE}${f.url}`;
        });
        setReady(true);
      })
      .catch(() => setReady(true));
  }, [sceneId]);

  // Keep the next few frames warm so the drive stays smooth throughout.
  useEffect(() => {
    for (let ahead = 1; ahead <= 6; ahead++) {
      const next = frames[index + ahead];
      if (next) {
        const img = new Image();
        img.src = `${API_BASE}${next.url}`;
      }
    }
  }, [index, frames]);

  useEffect(() => {
    if (!playing || frames.length === 0) return;
    timer.current = window.setInterval(() => {
      setIndex((i) => (i + 1) % frames.length);
    }, 110); // ~9 fps: reads as motion without burning through the corridor
    return () => {
      if (timer.current) window.clearInterval(timer.current);
    };
  }, [playing, frames.length]);

  const current = frames[index];
  const jump = useCallback(
    (value: number) => {
      setPlaying(false);
      setIndex(value);
    },
    [],
  );

  return (
    <div className="relative h-full w-full overflow-hidden bg-black">
      {current &&
        (anime ? (
          <AnimeFilter
            src={`${API_BASE}${current.url}`}
            className="h-full w-full object-cover"
          />
        ) : (
          <img
            src={`${API_BASE}${current.url}`}
            alt="80 Feet Road, Koramangala"
            className="h-full w-full object-cover"
            draggable={false}
          />
        ))}

      {!ready && (
        <div className="absolute inset-0 flex items-center justify-center">
          <span className="text-sm text-white/60">Loading street imagery…</span>
        </div>
      )}
      {ready && frames.length === 0 && (
        <div className="absolute inset-0 flex items-center justify-center">
          <span className="text-sm text-white/60">No street imagery on disk.</span>
        </div>
      )}

      {frames.length > 0 && (
        <div className="absolute bottom-3 left-1/2 flex w-[min(680px,92%)] -translate-x-1/2 items-center gap-3 rounded-xl border border-white/10 bg-black/80 px-3 py-2 backdrop-blur">
          <button
            onClick={() => setPlaying((p) => !p)}
            className="rounded-md border border-white/15 px-3 py-1 text-xs text-white/85 hover:bg-white/5"
          >
            {playing ? "❚❚" : "▶"}
          </button>
          <input
            type="range"
            min={0}
            max={frames.length - 1}
            value={index}
            onChange={(e) => jump(Number(e.target.value))}
            className="flex-1 accent-sky-400"
          />
          <span className="w-32 shrink-0 text-right text-[11px] tabular-nums text-white/50">
            frame {index + 1}/{frames.length}
            {loaded < 40 ? " · buffering" : ""}
          </span>
        </div>
      )}

      {current?.lat != null && (
        <div className="pointer-events-none absolute right-3 top-3 rounded-lg border border-white/10 bg-black/75 px-3 py-2 text-[11px] backdrop-blur">
          <div className="text-[10px] uppercase tracking-wider text-sky-300/70">
            Position
          </div>
          <div className="mt-0.5 tabular-nums text-white/80">
            {current.lat.toFixed(5)}, {current.lon?.toFixed(5)}
          </div>
          {current.compass != null && (
            <div className="text-[10px] text-white/40">
              heading {Math.round(current.compass)}°
            </div>
          )}
        </div>
      )}
    </div>
  );
}
