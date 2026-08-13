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
      } catch (exc) {
        if (!cancelled) setError(String(exc).slice(0, 400));
      }
    })();

    return () => {
      cancelled = true;
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
