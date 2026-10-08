import { useEdges, useNodes } from "@xyflow/react";
import { useEffect } from "react";

import { useMissingStartUrlStore } from "@/store/MissingStartUrlStore";

import { getNodeBrowserUrlError } from "../browserBlockUrl";
import type { AppNode } from "../nodes";
import { isMissingRequiredStartUrl } from "../workflowEditorUtils";

const MISSING_START_URL_MESSAGE =
  "Add a URL. No earlier block opens a page, so this block would start on a blank page.";

function blockUrlErrorId(blockId: string): string {
  return `${blockId}-url-error`;
}

function useBlockUrlError(blockId: string): string | null {
  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  const flagged = useMissingStartUrlStore((state) =>
    state.flaggedBlockIds.has(blockId),
  );
  const clear = useMissingStartUrlStore((state) => state.clear);
  const missing = isMissingRequiredStartUrl(nodes, edges, blockId);

  // Re-derived from the graph, so the flag also drops when an earlier
  // page-opening block is added, not only when a URL is typed.
  useEffect(() => {
    if (flagged && !missing) clear(blockId);
  }, [flagged, missing, clear, blockId]);

  if (flagged && missing) return MISSING_START_URL_MESSAGE;
  const node = nodes.find((node) => node.id === blockId);
  return node ? getNodeBrowserUrlError(node) : null;
}

export { blockUrlErrorId, useBlockUrlError };
