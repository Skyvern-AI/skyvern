import { useCallback, useEffect, useRef } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { toReadableSearch } from "@/routes/workflows/studio/panes";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";

import { type AppNode, isWorkflowBlockNode } from "../nodes";

export const SELECTED_BLOCK_SEARCH_PARAM = "selected-block";

function getStartNodeId(nodes: Array<AppNode>): string | null {
  return nodes.find((node) => node.type === "start")?.id ?? null;
}

// The URL key is the block's label, not its stable node id: renaming a block
// mid-session orphans any in-flight deep link, so a stale label falls back to
// the start node instead of resolving to the renamed block.
function getWorkflowBlockNodeIdByLabel(
  nodes: Array<AppNode>,
  blockLabel: string,
): string | null {
  return (
    nodes.find(
      (node) => isWorkflowBlockNode(node) && node.data.label === blockLabel,
    )?.id ?? null
  );
}

function getWorkflowBlockLabelByNodeId(
  nodes: Array<AppNode>,
  nodeId: string | null,
): string | null {
  if (nodeId === null) {
    return null;
  }

  const node = nodes.find((candidate) => candidate.id === nodeId);
  if (!node || !isWorkflowBlockNode(node)) {
    return null;
  }

  return node.data.label;
}

export function getInitialSelectedBlockId({
  enabled,
  nodes,
  searchParams,
}: {
  enabled: boolean;
  nodes: Array<AppNode>;
  searchParams: URLSearchParams;
}): string | null {
  if (!enabled) {
    return null;
  }

  const selectedBlockLabel = searchParams.get(SELECTED_BLOCK_SEARCH_PARAM);
  if (!selectedBlockLabel) {
    return getStartNodeId(nodes);
  }

  return (
    getWorkflowBlockNodeIdByLabel(nodes, selectedBlockLabel) ??
    getStartNodeId(nodes)
  );
}

type UseSelectedBlockUrlSyncOptions = {
  enabled: boolean;
  nodes: Array<AppNode>;
  getNodes?: () => Array<AppNode>;
};

export function useSelectedBlockUrlSync({
  enabled,
  nodes,
  getNodes,
}: UseSelectedBlockUrlSyncOptions): void {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const selectedBlockId = useWorkflowPanelStore((s) => s.selectedBlockId);
  const setSelectedBlockId = useWorkflowPanelStore((s) => s.setSelectedBlockId);
  const suppressNextSelectionMirrorRef = useRef(false);
  const selectedBlockLabelParam = searchParams.get(SELECTED_BLOCK_SEARCH_PARAM);
  const searchParamsRef = useRef(searchParams);
  searchParamsRef.current = searchParams;
  // Skip reconciling from the URL when its value hasn't changed: a
  // `nodes`-only re-run of the effect below would otherwise revert a
  // same-commit fresher store selection back to the stale URL target.
  const lastAppliedLabelParamRef = useRef<string | null>(null);

  const getCurrentNodes = useCallback(() => {
    const latestNodes = getNodes?.();
    return latestNodes && latestNodes.length > 0 ? latestNodes : nodes;
  }, [getNodes, nodes]);

  // Merge writes against the live URL, not this render's closure: pushState is
  // synchronous, so a concurrent navigate (pane toggles writing ?panes=) is
  // already visible there while the closure params can be one render stale and
  // would clobber it. window.location is blank under a memory router (tests);
  // fall back to the closure, where no such race exists.
  const liveParams = useCallback(
    () =>
      window.location.search !== ""
        ? new URLSearchParams(window.location.search)
        : new URLSearchParams(searchParamsRef.current),
    [],
  );

  useEffect(() => {
    if (!enabled) {
      return;
    }
    if (!selectedBlockLabelParam) {
      lastAppliedLabelParamRef.current = null;
      return;
    }
    if (selectedBlockLabelParam === lastAppliedLabelParamRef.current) {
      return;
    }
    lastAppliedLabelParamRef.current = selectedBlockLabelParam;

    const currentNodes = getCurrentNodes();
    const matchedNodeId = getWorkflowBlockNodeIdByLabel(
      currentNodes,
      selectedBlockLabelParam,
    );
    if (!matchedNodeId) {
      const next = liveParams();
      next.delete(SELECTED_BLOCK_SEARCH_PARAM);
      navigate({ search: toReadableSearch(next) }, { replace: true });
    }

    // Must actually move selectedBlockId off the stale block, not just skip
    // this mirror cycle: leaving it pointed at the old (still valid)
    // selection means the very next mirror run recomputes that block's
    // label and writes it straight back into the just-cleared URL.
    const targetNodeId = matchedNodeId ?? getStartNodeId(currentNodes);
    const currentSelectedBlockId =
      useWorkflowPanelStore.getState().selectedBlockId;
    if (currentSelectedBlockId !== targetNodeId) {
      suppressNextSelectionMirrorRef.current = true;
      setSelectedBlockId(targetNodeId);
    }
  }, [
    enabled,
    getCurrentNodes,
    liveParams,
    navigate,
    selectedBlockLabelParam,
    setSelectedBlockId,
  ]);

  useEffect(() => {
    if (!enabled) {
      return;
    }

    // Relies on the URL->store effect above running first each render and
    // setting this flag when it moves selectedBlockId; if the two effects
    // are ever reordered, this suppression stops working.
    if (suppressNextSelectionMirrorRef.current) {
      suppressNextSelectionMirrorRef.current = false;
      return;
    }

    const selectedBlockLabel = getWorkflowBlockLabelByNodeId(
      getCurrentNodes(),
      selectedBlockId,
    );
    const currentUrlLabel = searchParams.get(SELECTED_BLOCK_SEARCH_PARAM);
    if (currentUrlLabel === selectedBlockLabel) {
      return;
    }

    const next = liveParams();
    if (selectedBlockLabel) {
      next.set(SELECTED_BLOCK_SEARCH_PARAM, selectedBlockLabel);
    } else {
      next.delete(SELECTED_BLOCK_SEARCH_PARAM);
    }
    navigate({ search: toReadableSearch(next) }, { replace: true });
  }, [
    enabled,
    getCurrentNodes,
    liveParams,
    navigate,
    searchParams,
    selectedBlockId,
  ]);
}
