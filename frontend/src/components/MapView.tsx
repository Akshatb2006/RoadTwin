"use client";

import { useEffect, useMemo, useRef, useState } from "react";
// Pinned to maplibre-gl v5 deliberately. v6 is ESM-only and loads its worker
// from a separate maplibre-gl-worker.mjs, which Turbopack does not resolve --
// the worker silently fails to start, so no source ever finishes loading, the
// style stays permanently unloaded, and the map renders an empty canvas with
// no error. v5 bundles the worker inline.
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import type { Bottleneck, PlaybackFrame, SegmentMetric } from "@/lib/api";
import { CLASS_COLOURS } from "@/lib/api";

type Props = {
  geometry: { roads: GeoJSON.FeatureCollection; junctions: GeoJSON.FeatureCollection } | null;
  center: { lat: number; lon: number } | null;
  segmentMetrics: SegmentMetric[];
  bottlenecks: Bottleneck[];
  frames: PlaybackFrame[];
  playing: boolean;
  frameIndex: number;
  onFrameChange: (index: number) => void;
  showBasemap: boolean;
};

const EMPTY: GeoJSON.FeatureCollection = { type: "FeatureCollection", features: [] };

/** Congestion colour ramp keyed on speed ratio (mean / free-flow). */
const CONGESTION_RAMP: maplibregl.ExpressionSpecification = [
  "interpolate",
  ["linear"],
  ["coalesce", ["get", "speed_ratio"], 1],
  0.0, "#b3122b",
  0.25, "#e8482c",
  0.45, "#f5a524",
  0.65, "#d8d34a",
  0.85, "#4ec27a",
  1.0, "#2f9e5f",
];

export default function MapView({
  geometry,
  center,
  segmentMetrics,
  bottlenecks,
  frames,
  playing,
  frameIndex,
  onFrameChange,
  showBasemap,
}: Props) {
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<maplibregl.Map | null>(null);
  const [ready, setReady] = useState(false);
  const [hovered, setHovered] = useState<Record<string, unknown> | null>(null);
  const frameRef = useRef(frameIndex);
  const rafRef = useRef<number | null>(null);

  // ---------------------------------------------------------------- init
  useEffect(() => {
    if (!container.current || map.current) return;

    const instance = new maplibregl.Map({
      container: container.current,
      style: {
        version: 8,
        // A blank canvas by default: the demo must not depend on a tile server
        // being reachable. The basemap is opt-in and additive.
        sources: {},
        layers: [
          {
            id: "background",
            type: "background",
            paint: { "background-color": "#080a10" },
          },
        ],
      },
      center: [center?.lon ?? 77.62, center?.lat ?? 12.935],
      zoom: 14,
      attributionControl: false,
maxPitch: 60,
    });

    instance.addControl(new maplibregl.NavigationControl({ showCompass: true }), "top-right");

    // Debug handle: `__rtmap.getStyle().layers` from the console is the fastest
    // way to tell "layers never got added" from "layers have no data".
    (window as unknown as { __rtmap?: maplibregl.Map }).__rtmap = instance;
    // Surface style/tile errors instead of letting them fail silently.
    instance.on("error", (event) => console.error("[maplibre]", event.error?.message));

    instance.on("load", () => {
      instance.addSource("roads", { type: "geojson", data: EMPTY });
      instance.addSource("junctions", { type: "geojson", data: EMPTY });
      instance.addSource("vehicles", { type: "geojson", data: EMPTY });
      instance.addSource("bottlenecks", { type: "geojson", data: EMPTY });

      // Wide soft casing underneath gives the network depth on a dark canvas.
      instance.addLayer({
        id: "roads-casing",
        type: "line",
        source: "roads",
        paint: {
          "line-color": "#0d1522",
          "line-width": [
            "interpolate", ["linear"], ["zoom"],
            12, ["*", ["coalesce", ["get", "lanes"], 1], 1.2],
            17, ["*", ["coalesce", ["get", "lanes"], 1], 4.5],
          ],
          "line-opacity": 0.9,
        },
        layout: { "line-cap": "round", "line-join": "round" },
      });

      instance.addLayer({
        id: "roads",
        type: "line",
        source: "roads",
        paint: {
          "line-color": CONGESTION_RAMP,
          "line-width": [
            "interpolate", ["linear"], ["zoom"],
            12, ["*", ["coalesce", ["get", "lanes"], 1], 0.7],
            17, ["*", ["coalesce", ["get", "lanes"], 1], 3.0],
          ],
          "line-opacity": 0.95,
        },
        layout: { "line-cap": "round", "line-join": "round" },
      });

      instance.addLayer({
        id: "junctions",
        type: "circle",
        source: "junctions",
        paint: {
          "circle-radius": [
            "interpolate", ["linear"], ["zoom"],
            13, 1.2,
            17, ["case", ["==", ["get", "type"], "signalised"], 5, 2.4],
          ],
          "circle-color": [
            "case",
            ["==", ["get", "type"], "signalised"], "#5ad1ff",
            ["==", ["get", "type"], "roundabout"], "#c78bff",
            "#3a4a63",
          ],
          "circle-opacity": 0.85,
        },
      });

      // Pulsing halo marks the ranked bottlenecks.
      instance.addLayer({
        id: "bottleneck-halo",
        type: "circle",
        source: "bottlenecks",
        paint: {
          "circle-radius": ["interpolate", ["linear"], ["zoom"], 13, 10, 17, 30],
          "circle-color": "#ff2d55",
          "circle-opacity": ["*", 0.18, ["coalesce", ["get", "severity"], 0.5]],
          "circle-blur": 0.7,
        },
      });
      instance.addLayer({
        id: "bottleneck-core",
        type: "circle",
        source: "bottlenecks",
        paint: {
          "circle-radius": 6,
          "circle-color": "#ff2d55",
          "circle-stroke-color": "#fff",
          "circle-stroke-width": 1.5,
        },
      });

      instance.addLayer({
        id: "vehicles",
        type: "circle",
        source: "vehicles",
        paint: {
          "circle-radius": [
            "interpolate", ["linear"], ["zoom"],
            13, 1.6,
            16, ["match", ["get", "c"], "b", 5, "t", 4.5, "m", 2.4, 3.2],
            18, ["match", ["get", "c"], "b", 9, "t", 8, "m", 4, 6],
          ],
          "circle-color": [
            "match", ["get", "c"],
            "c", CLASS_COLOURS.c, "m", CLASS_COLOURS.m, "a", CLASS_COLOURS.a,
            "b", CLASS_COLOURS.b, "t", CLASS_COLOURS.t, "y", CLASS_COLOURS.y,
            "#ffffff",
          ],
          "circle-stroke-color": "#05070c",
          "circle-stroke-width": 0.5,
        },
      });

      instance.on("mousemove", "roads", (event: maplibregl.MapLayerMouseEvent) => {
        instance.getCanvas().style.cursor = "pointer";
        setHovered(event.features?.[0]?.properties ?? null);
      });
      instance.on("mouseleave", "roads", () => {
        instance.getCanvas().style.cursor = "";
        setHovered(null);
      });

      setReady(true);
    });

    map.current = instance;
    return () => {
      instance.remove();
      map.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ------------------------------------------------------- optional basemap
  useEffect(() => {
    const instance = map.current;
    if (!instance || !ready) return;
    const hasSource = !!instance.getSource("basemap");
    if (showBasemap && !hasSource) {
      instance.addSource("basemap", {
        type: "raster",
        tiles: ["https://basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png"],
        tileSize: 256,
        attribution: "© OpenStreetMap contributors © CARTO",
      });
      instance.addLayer(
        { id: "basemap", type: "raster", source: "basemap", paint: { "raster-opacity": 0.45 } },
        "roads-casing",
      );
    } else if (!showBasemap && hasSource) {
      if (instance.getLayer("basemap")) instance.removeLayer("basemap");
      instance.removeSource("basemap");
    }
  }, [showBasemap, ready]);

  // ---------------------------------------------------------- road geometry
  // Merge per-segment simulation results into the rendered features so the
  // network itself becomes the congestion heatmap.
  const roadData = useMemo(() => {
    if (!geometry) return EMPTY;
    if (!segmentMetrics.length) return geometry.roads;
    const ratios = new Map(segmentMetrics.map((m) => [m.segment_id, m]));
    return {
      ...geometry.roads,
      features: geometry.roads.features.map((feature) => {
        const metric = ratios.get(feature.properties?.id as string);
        return metric
          ? {
              ...feature,
              properties: {
                ...feature.properties,
                speed_ratio: metric.speed_ratio,
                mean_speed_kmh: metric.mean_speed_kmh,
                queue_m: metric.max_queue_m,
              },
            }
          : feature;
      }),
    } as GeoJSON.FeatureCollection;
  }, [geometry, segmentMetrics]);

  useEffect(() => {
    const instance = map.current;
    if (!instance || !ready) return;
    (instance.getSource("roads") as maplibregl.GeoJSONSource | undefined)?.setData(roadData);
    (instance.getSource("junctions") as maplibregl.GeoJSONSource | undefined)?.setData(
      geometry?.junctions ?? EMPTY,
    );
  }, [roadData, geometry, ready]);

  // Fit to the network the first time it arrives.
  const fitted = useRef(false);
  useEffect(() => {
    const instance = map.current;
    if (!instance || !ready || !geometry || fitted.current) return;
    const bounds = new maplibregl.LngLatBounds();
    let count = 0;
    for (const feature of geometry.roads.features) {
      if (feature.geometry.type !== "LineString") continue;
      for (const coord of feature.geometry.coordinates) {
        bounds.extend(coord as [number, number]);
        count++;
      }
      if (count > 8000) break;
    }
    if (count) {
      instance.fitBounds(bounds, { padding: 60, duration: 900 });
      fitted.current = true;
    }
  }, [geometry, ready]);

  // ------------------------------------------------------------ bottlenecks
  useEffect(() => {
    const instance = map.current;
    if (!instance || !ready) return;
    (instance.getSource("bottlenecks") as maplibregl.GeoJSONSource | undefined)?.setData({
      type: "FeatureCollection",
      features: bottlenecks
        .filter((b) => b.lat && b.lon)
        .map((b) => ({
          type: "Feature" as const,
          geometry: { type: "Point" as const, coordinates: [b.lon, b.lat] },
          properties: { name: b.name, severity: b.severity, rank: b.rank },
        })),
    });
  }, [bottlenecks, ready]);

  // -------------------------------------------------------------- playback
  useEffect(() => {
    frameRef.current = frameIndex;
    const instance = map.current;
    if (!instance || !ready) return;
    const frame = frames[frameIndex];
    (instance.getSource("vehicles") as maplibregl.GeoJSONSource | undefined)?.setData({
      type: "FeatureCollection",
      features: (frame?.v ?? []).map(([lon, lat, angle, speed, cls]) => ({
        type: "Feature" as const,
        geometry: { type: "Point" as const, coordinates: [lon, lat] },
        properties: { c: cls, a: angle, s: speed },
      })),
    });
  }, [frames, frameIndex, ready]);

  // Animation clock. ~8 fps reads as traffic without burning the main thread.
  useEffect(() => {
    if (!playing || frames.length === 0) return;
    let last = performance.now();
    const tick = (now: number) => {
      if (now - last > 125) {
        last = now;
        onFrameChange((frameRef.current + 1) % frames.length);
      }
      rafRef.current = requestAnimationFrame(tick);
    };
    rafRef.current = requestAnimationFrame(tick);
    return () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
    };
  }, [playing, frames.length, onFrameChange]);

  return (
    <div className="relative h-full w-full">
      <div ref={container} className="h-full w-full" />

      {hovered && (
        <div className="pointer-events-none absolute left-3 top-3 max-w-xs rounded-lg border border-white/10 bg-black/80 px-3 py-2 text-xs backdrop-blur">
          <div className="font-medium text-white">
            {(hovered.name as string) || "Unnamed road"}
          </div>
          <div className="mt-1 grid grid-cols-2 gap-x-3 gap-y-0.5 text-white/60">
            <span>Class</span>
            <span className="text-white/90">{hovered.road_class as string}</span>
            <span>Lanes</span>
            <span className="text-white/90">
              {hovered.lanes as number}
              {hovered.effective_lanes ? ` (eff. ${hovered.effective_lanes})` : ""}
            </span>
            <span>Speed limit</span>
            <span className="text-white/90">{hovered.speed_limit_kmh as number} km/h</span>
            {hovered.capacity_pcu_hr ? (
              <>
                <span>Capacity</span>
                <span className="text-white/90">
                  {Math.round(hovered.capacity_pcu_hr as number)} PCU/hr
                </span>
              </>
            ) : null}
            {hovered.mean_speed_kmh !== undefined ? (
              <>
                <span>Simulated</span>
                <span className="text-white/90">
                  {(hovered.mean_speed_kmh as number).toFixed(1)} km/h
                </span>
              </>
            ) : null}
          </div>
        </div>
      )}

      {segmentMetrics.length > 0 && (
        // Sits above the playback transport bar, which is pinned to bottom-3.
        <div className="pointer-events-none absolute bottom-16 left-3 rounded-lg border border-white/10 bg-black/75 px-3 py-2 text-[11px] backdrop-blur">
          <div className="mb-1 text-white/50">Speed vs free-flow</div>
          <div className="flex items-center gap-2">
            <span className="text-white/60">0%</span>
            <div
              className="h-2 w-32 rounded"
              style={{
                background:
                  "linear-gradient(90deg,#b3122b,#e8482c,#f5a524,#d8d34a,#4ec27a,#2f9e5f)",
              }}
            />
            <span className="text-white/60">100%</span>
          </div>
        </div>
      )}
    </div>
  );
}
