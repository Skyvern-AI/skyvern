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

function isStudioPaneId(value: string): value is StudioPaneId {
  return (STUDIO_PANE_IDS as readonly string[]).includes(value);
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
export function panesFromDeepLink(params: {
  runId: string | null;
  active: string | null;
  blockLabel: string | null;
}): StudioPaneId[] {
  if (params.runId && params.blockLabel) {
    return ["run", "browser"];
  }
  if (params.runId || params.active) {
    return ["run"];
  }
  return [...DEFAULT_STUDIO_PANES];
}

// Open panes for a studio search string. An explicit ?panes= wins over the
// legacy deep-link params (?wr= / ?active= / ?bl=).
export function resolveOpenPanes(search: string): StudioPaneId[] {
  const params = new URLSearchParams(search);
  const explicit = parsePanesParam(params.get(STUDIO_PANES_PARAM));
  if (explicit !== null) {
    return explicit;
  }
  return panesFromDeepLink({
    runId: params.get("wr"),
    active: params.get("active"),
    blockLabel: params.get("bl"),
  });
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
