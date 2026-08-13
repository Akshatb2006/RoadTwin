// The library ships no type declarations. We touch only a handful of members,
// so a minimal ambient declaration is preferable to widening the call site to
// `any` or excluding the module from type checking.
declare module "@mkkellogg/gaussian-splats-3d" {
  export const SceneFormat: { Ply: number; Splat: number; KSplat: number };

  export class Viewer {
    constructor(options: Record<string, unknown>);
    addSplatScene(url: string, options?: Record<string, unknown>): Promise<void>;
    start(): void;
    dispose(): void;
  }
}
