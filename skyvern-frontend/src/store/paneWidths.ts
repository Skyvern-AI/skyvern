// Keys are studio pane ids; kept as plain strings so the store stays agnostic
// to pane renames (stale keys are simply never read).
export type PaneWidths = Record<string, number>;

// Pane widths round-trip through localStorage, so treat each value as
// untrusted. Lives at the store layer so both StudioShellStore (persistence)
// and the studio layout math share one validation contract.
export function sanitizePaneWidth(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) && value > 0
    ? Math.round(value)
    : undefined;
}
