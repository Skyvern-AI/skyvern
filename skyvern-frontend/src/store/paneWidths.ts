// Keys are studio pane ids; kept as plain strings so the store stays agnostic
// to pane renames (stale keys are simply never read).
export type PaneWidths = Record<string, number>;

export function sanitizePaneWidth(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) && value > 0
    ? Math.round(value)
    : undefined;
}
