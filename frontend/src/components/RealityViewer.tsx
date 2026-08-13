"use client";

import { useEffect, useRef, useState } from "react";
import { API_BASE } from "@/lib/api";

type Props = { sceneId: string };

export default function RealityViewer({ sceneId }: Props) {
  const container = useRef<HTMLDivElement>(null);
  const viewerRef = useRef<{ dispose?: () => void } | null>(null);
  const [status, setStatus] = useState("Loading reconstruction…");
  const [error, setError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const [cameraCount, setCameraCount] = useState(0);
  const [driving, setDriving] = useState(true);
  const posesRef = useRef<Array<{ C: number[]; fwd: number[] }>>([]);
  const driveRef = useRef({ t: 0, raf: 0 as number, driving: true });

  useEffect(() => {
    let cancelled = false;
    let viewer: any = null; // eslint-disable-line @typescript-eslint/no-explicit-any

    (async () => {
      if (!container.current) return;
      try {
        // Imported dynamically: the library touches WebGL and Workers at module
        // scope, so it cannot be evaluated during SSR.
        const GS = await import("@mkkellogg/gaussian-splats-3d");
        if (cancelled) return;

        // Start at an actual reconstructed camera pose. An arbitrary origin
        // puts the camera inside the Gaussian cloud, which renders as blobs
        // rather than a street.
        let position = [0, 0, -1];
        let lookAt = [0, 0, 0];
        try {
          const cams = await fetch(`${API_BASE}/api/reality/${sceneId}/cameras`).then(
            (r) => r.json(),
          );
          if (cams?.initial) {
            position = cams.initial.position;
            lookAt = cams.initial.lookAt;
            setCameraCount(cams.count ?? 0);
            posesRef.current = cams.cameras ?? [];
          }
        } catch {
          /* fall back to the origin view */
        }

        viewer = new GS.Viewer({
          rootElement: container.current,
          // COLMAP is Y-down; without this the street loads upside down.
          cameraUp: [0, -1, 0],
          initialCameraPosition: position,
          initialCameraLookAt: lookAt,
          // Avoids requiring COOP/COEP cross-origin isolation headers, which
          // Next's dev server does not send.
          // A narrower field of view keeps the frame on the well-constrained
          // centre of the corridor and pushes the poorly-reconstructed
          // periphery out of shot. It also reads as a longer lens, which suits
          // a street scene.
          camera: undefined,
          sharedMemoryForWorkers: false,
          dynamicScene: false,
          selfDrivenMode: true,
          useBuiltInControls: true,
        });
        viewerRef.current = viewer;

        setStatus("Streaming 52 MB splat…");
        await viewer.addSplatScene(`${API_BASE}/api/reality/${sceneId}/splat`, {
          format: GS.SceneFormat.Ply,
          showLoadingUI: false,
          progressiveLoad: true,
        });
        if (cancelled) return;
        viewer.start();
        setReady(true);
        setStatus("");

        // Drive the camera along the reconstructed trajectory instead of
        // offering free orbit. The imagery is forward-motion only, so surfaces
        // are well constrained along the driven path and badly constrained
        // away from it -- a free camera asks the splat questions the source
        // photographs never answered. Riding the path shows the reconstruction
        // where it is strongest, and is the natural shot for a road anyway.
        const poses = posesRef.current;
        if (poses.length > 2) {
          const lerp = (a: number[], b: number[], t: number) =>
            a.map((v, i) => v + (b[i] - v) * t);
          const step = () => {
            driveRef.current.raf = requestAnimationFrame(step);
            if (!driveRef.current.driving) return;
            // ~9 s for the full corridor, looping.
            // Slower than the first pass: the reconstruction reads better when
            // the viewer is not rushed past it.
            driveRef.current.t = (driveRef.current.t + 0.0013) % 1;
            const f = driveRef.current.t * (poses.length - 1);
            const i = Math.floor(f);
            const frac = f - i;
            const a = poses[i];
            const b = poses[Math.min(i + 1, poses.length - 1)];
            const C = lerp(a.C, b.C, frac);
            const fwd = lerp(a.fwd, b.fwd, frac);
            const target = C.map((v, k) => v + fwd[k] * 6);
            try {
              if (viewer.camera.fov !== 42) {
                viewer.camera.fov = 42;
                viewer.camera.updateProjectionMatrix();
              }
              viewer.camera.position.set(C[0], C[1], C[2]);
              viewer.camera.lookAt(target[0], target[1], target[2]);
              if (viewer.controls) {
                viewer.controls.target.set(target[0], target[1], target[2]);
              }
            } catch {
              /* viewer torn down mid-frame */
            }
          };
          driveRef.current.raf = requestAnimationFrame(step);
        }
      } catch (exc) {
        if (!cancelled) setError(String(exc).slice(0, 400));
      }
    })();

    return () => {
      cancelled = true;
      if (driveRef.current.raf) cancelAnimationFrame(driveRef.current.raf);
      try {
        viewer?.dispose?.();
      } catch {
        /* viewer may not have initialised */
      }
    };
  }, [sceneId]);

  return (
    <div className="relative h-full w-full bg-[#05070c]">
      <div ref={container} className="h-full w-full" />

      {!ready && !error && (
        <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
          <div className="rounded-xl border border-white/10 bg-black/75 px-5 py-4 text-center backdrop-blur">
            <div className="mx-auto mb-2 h-4 w-4 animate-spin rounded-full border-[1.5px] border-white/25 border-t-sky-400" />
            <p className="text-sm text-white/80">{status}</p>
            <p className="mt-1 text-[11px] text-white/40">
              211,772 Gaussians · {cameraCount || 44} reconstructed camera poses
            </p>
          </div>
        </div>
      )}

      {ready && cameraCount > 0 && (
        <div className="absolute bottom-3 left-1/2 flex -translate-x-1/2 items-center gap-3 rounded-xl border border-white/10 bg-black/80 px-3 py-2 backdrop-blur">
          <button
            onClick={() => {
              driveRef.current.driving = !driveRef.current.driving;
              setDriving(driveRef.current.driving);
            }}
            className="rounded-md border border-white/15 px-3 py-1 text-xs text-white/80 hover:bg-white/5"
          >
            {driving ? "❚❚ Pause drive" : "▶ Drive corridor"}
          </button>
          <span className="text-[11px] text-white/45">
            Camera rides the {cameraCount} reconstructed poses
          </span>
        </div>
      )}

      {error && (
        <div className="absolute inset-0 flex items-center justify-center p-6">
          <div className="max-w-lg rounded-xl border border-rose-500/30 bg-rose-500/10 px-4 py-3">
            <p className="text-sm text-rose-200">Could not load the reconstruction.</p>
            <pre className="mt-2 whitespace-pre-wrap text-[10px] text-rose-200/70">
              {error}
            </pre>
          </div>
        </div>
      )}
    </div>
  );
}
