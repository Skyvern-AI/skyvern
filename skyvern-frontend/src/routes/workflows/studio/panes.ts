export type StudioPaneId = "copilot" | "editor" | "browser" | "overview";

export const STUDIO_PANE_IDS: readonly StudioPaneId[] = [
  "copilot",
  "editor",
  "browser",
  "overview",
];

export const STUDIO_PANES_PARAM = "panes";

// Accepted forever on parse so pre-rename ?panes= links keep working; the
// canonical id ("overview") is what serializes back out.
const STUDIO_PANE_ID_ALIASES: Record<string, StudioPaneId> = {
  run: "overview",
  timeline: "overview",
};

export const DEFAULT_STUDIO_PANES: readonly StudioPaneId[] = [
  "copilot",
  "browser",
];

// In-app run starts (full run or block ▶) append the run surfaces to whatever
// is already open — they never rearrange or close panes.
export const RUN_APPEND_PANES: readonly StudioPaneId[] = [
  "browser",
  "overview",
];

// Panes that mutate the workflow (Copilot builds, Editor saves) are blocked
// while the shell shows a run of a deleted agent; run viewing stays.
export const DELETED_WORKFLOW_BLOCKED_PANES: readonly StudioPaneId[] = [
  "copilot",
  "editor",
];

export function panesWithoutDeletedBlocked(
  panes: readonly StudioPaneId[],
): StudioPaneId[] {
  return panes.filter((id) => !DELETED_WORKFLOW_BLOCKED_PANES.includes(id));
}

// Copilot / Editor / Overview share one narrow floor; the browser viewport
// keeps a little more room. The stage clamps shared links and nudges on
// over-tight opens against these numbers (fitPanesToWidth below), and divider
// resizes clamp against them too.
export const STUDIO_PANE_MIN_WIDTH: Record<StudioPaneId, number> = {
  copilot: 260,
  editor: 260,
  browser: 300,
  overview: 260,
};

// Stage chrome for the fit math; must match the stage p-3 and the divider
// width (the resize dividers are the inter-pane gap) in StudioShell.tsx.
export const STUDIO_STAGE_PADDING_PX = 24;
export const STUDIO_STAGE_GAP_PX = 12;

function isStudioPaneId(value: string): value is StudioPaneId {
  return (STUDIO_PANE_IDS as readonly string[]).includes(value);
}

// Cold-entry defaults when no deep link decides: an empty agent starts on
// prompt-and-watch (Copilot builds, the Browser shows it work — the Editor
// auto-appends once a build lands); a built agent adds the Editor.
export function defaultPanesForWorkflowState(state: {
  hasBlocks: boolean;
}): StudioPaneId[] {
  return state.hasBlocks
    ? ["copilot", "browser", "editor"]
    : [...DEFAULT_STUDIO_PANES];
}

export function panesListEqual(
  a: readonly StudioPaneId[],
  b: readonly StudioPaneId[],
): boolean {
  return a.length === b.length && a.every((id, index) => id === b[index]);
}

export function panesFitWidth(
  panes: readonly StudioPaneId[],
  stageWidth: number,
): boolean {
  if (panes.length === 0) {
    return true;
  }
  const total =
    panes.reduce((sum, id) => sum + STUDIO_PANE_MIN_WIDTH[id], 0) +
    STUDIO_STAGE_PADDING_PX +
    STUDIO_STAGE_GAP_PX * (panes.length - 1);
  return total <= stageWidth;
}

// Degrade an over-wide open list to its longest leading prefix that fits at
// min-widths; the first pane always survives so a link never lands on nothing.
export function fitPanesToWidth(
  panes: readonly StudioPaneId[],
  stageWidth: number,
): StudioPaneId[] {
  const kept: StudioPaneId[] = [];
  for (const id of panes) {
    if (!panesFitWidth([...kept, id], stageWidth)) {
      break;
    }
    kept.push(id);
  }
  if (kept.length === 0 && panes.length > 0) {
    kept.push(panes[0]!);
  }
  return kept;
}

// Ordered open-pane list from an explicit ?panes= value; unknown entries and
// duplicates are dropped. null means the param was absent (callers fall back to
// the deep-link mapping); an empty value is an explicit "no panes open".
export function parsePanesParam(raw: string | null): StudioPaneId[] | null {
  if (raw === null) {
    return null;
  }
  const result: StudioPaneId[] = [];
  for (const token of raw.split(",")) {
    const name = token.trim();
    const id = STUDIO_PANE_ID_ALIASES[name] ?? name;
    if (isStudioPaneId(id) && !result.includes(id)) {
      result.push(id);
    }
  }
  return result;
}

// Deep-link → panes mapping when ?panes= is absent: a block-run link lands on
// iterate (Editor leads); any other run reference lands on watch-and-review.
export function panesFromDeepLink(
  params: {
    runId: string | null;
    active: string | null;
    blockLabel: string | null;
  },
  defaultPanes: readonly StudioPaneId[] = DEFAULT_STUDIO_PANES,
): StudioPaneId[] {
  if (params.runId && params.blockLabel) {
    return ["editor", "browser", "overview"];
  }
  if (params.runId || params.active) {
    return ["copilot", "browser", "overview"];
  }
  return [...defaultPanes];
}

// Open panes for a studio search string. An explicit ?panes= wins over the
// legacy deep-link params (?wr= / ?active= / ?bl=); both win over defaultPanes.
export function resolveOpenPanes(
  search: string,
  defaultPanes: readonly StudioPaneId[] = DEFAULT_STUDIO_PANES,
): StudioPaneId[] {
  const params = new URLSearchParams(search);
  const explicit = parsePanesParam(params.get(STUDIO_PANES_PARAM));
  if (explicit !== null) {
    return explicit;
  }
  return panesFromDeepLink(
    {
      runId: params.get("wr"),
      active: params.get("active"),
      blockLabel: params.get("bl"),
    },
    defaultPanes,
  );
}

// Open panes append, close panes splice: list order is the layout order.
export function togglePane(
  panes: readonly StudioPaneId[],
  id: StudioPaneId,
): StudioPaneId[] {
  return panes.includes(id) ? panes.filter((p) => p !== id) : [...panes, id];
}

export function withPaneOpen(
  panes: readonly StudioPaneId[],
  id: StudioPaneId,
): StudioPaneId[] {
  return panes.includes(id) ? [...panes] : [...panes, id];
}

export function withPanesOpen(
  panes: readonly StudioPaneId[],
  ids: readonly StudioPaneId[],
): StudioPaneId[] {
  return ids.reduce<StudioPaneId[]>(
    (acc, id) => withPaneOpen(acc, id),
    [...panes],
  );
}

export function withPaneClosed(
  panes: readonly StudioPaneId[],
  id: StudioPaneId,
): StudioPaneId[] {
  return panes.filter((p) => p !== id);
}

// Commas are legal unencoded in query values and parse back identically; keep
// ?panes=copilot,browser readable no matter which writer serialized last.
export function toReadableSearch(params: URLSearchParams): string {
  const raw = params.toString().replace(/%2C/g, ",");
  return raw ? `?${raw}` : "";
}

// Serialize the open list into a search string, preserving unrelated params.
export function searchWithPanes(
  search: string,
  panes: readonly StudioPaneId[],
): string {
  const params = new URLSearchParams(search);
  params.set(STUDIO_PANES_PARAM, panes.join(","));
  return toReadableSearch(params);
}
