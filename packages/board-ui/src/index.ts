// Public surface of @celeborn/board-ui. Grows phase by phase (see plan/cele-t98-shared-board-ui.md §4).
// Phase 0 (CELE-t112): shared types only — proves the workspace + transpilePackages pipe.
// Phase 1 (CELE-t112): SandProgress (the byte-identical component) moves here.

export type { TaskState } from "./types";

export { SandProgress, type SandStatus } from "./SandProgress";
