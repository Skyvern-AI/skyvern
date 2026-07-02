export type StudioPaneId = "copilot" | "editor" | "browser" | "run";

export const STUDIO_PANE_IDS: readonly StudioPaneId[] = [
  "copilot",
  "editor",
  "browser",
  "run",
];

export const STUDIO_PANES_PARAM = "panes";

export const DEFAULT_STUDIO_PANES: readonly StudioPaneId[] = [
  "copilot",
  "browser",
];

// Width floors from the approved mock; the stage clamps shared links and nudges
// on over-tight opens against these same numbers (fitPanesToWidth below).
export const STUDIO_PANE_MIN_WIDTH: Record<StudioPaneId, number> = {
  copilot: 260,
  editor: 220,
  browser: 260,
  run: 220,
};

// Stage chrome for the fit math; must match the p-3 + gap-3 on the stage div
// in StudioShell.tsx.
export const STUDIO_STAGE_PADDING_PX = 24;
export const STUDIO_STAGE_GAP_PX = 12;

function isStudioPaneId(value: string): value is StudioPaneId {
  return (STUDIO_PANE_IDS as readonly string[]).includes(value);
}

// First-visit defaults: someone who never ran the agent (or has nothing built)
// starts on the familiar build surface; an agent with history starts on watch.
export function defaultPanesForWorkflowState(state: {
  hasRuns: boolean | undefined;
  hasBlocks: boolean;
}): StudioPaneId[] {
  if (state.hasRuns !== undefined) {
    return state.hasRuns ? ["copilot", "browser"] : ["copilot", "editor"];
  }
  return state.hasBlocks ? [...DEFAULT_STUDIO_PANES] : ["copilot", "editor"];
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
    const id = token.trim();
    if (isStudioPaneId(id) && !result.includes(id)) {
      result.push(id);
    }
  }
  return result;
}

// Deep-link → panes mapping when ?panes= is absent: a block run shows its
// timeline beside the live debug stream; any other run reference lands on Run.
export function panesFromDeepLink(
  params: {
    runId: string | null;
    active: string | null;
    blockLabel: string | null;
  },
  defaultPanes: readonly StudioPaneId[] = DEFAULT_STUDIO_PANES,
): StudioPaneId[] {
  if (params.runId && params.blockLabel) {
    return ["run", "browser"];
  }
  if (params.runId || params.active) {
    return ["run"];
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

// Serialize the open list into a search string, preserving unrelated params.
export function searchWithPanes(
  search: string,
  panes: readonly StudioPaneId[],
): string {
  const params = new URLSearchParams(search);
  params.set(STUDIO_PANES_PARAM, panes.join(","));
  // Commas are legal unencoded in query values and parse back identically;
  // keep them readable (?panes=copilot,browser).
  return `?${params.toString().replace(/%2C/g, ",")}`;
}
