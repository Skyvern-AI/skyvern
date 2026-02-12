import { useEffect, useMemo, useRef } from "react";
import { useDebouncedCallback } from "use-debounce";
import type { Edge } from "@xyflow/react";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useWorkflowTitleStore } from "@/store/WorkflowTitleStore";
import { getWorkflowBlocks } from "../editor/workflowEditorUtils";
import type { AppNode } from "../editor/nodes";
import type { BlockYAML } from "../types/workflowYamlTypes";

type BlockInfo = {
  block_type: string;
  url?: string;
  goal?: string;
};

function extractBlockInfo(block: BlockYAML): BlockInfo {
  const info: BlockInfo = { block_type: block.block_type };

  if ("url" in block && block.url) {
    info.url = block.url;
  }

  if ("navigation_goal" in block && block.navigation_goal) {
    info.goal =
      block.navigation_goal.length > 150
        ? block.navigation_goal.slice(0, 150)
        : block.navigation_goal;
  } else if ("data_extraction_goal" in block && block.data_extraction_goal) {
    info.goal =
      block.data_extraction_goal.length > 150
        ? block.data_extraction_goal.slice(0, 150)
        : block.data_extraction_goal;
  } else if ("prompt" in block && block.prompt) {
    const prompt = block.prompt;
    info.goal = prompt.length > 150 ? prompt.slice(0, 150) : prompt;
  }

  return info;
}

function hasMeaningfulContent(blocksInfo: BlockInfo[]): boolean {
  return blocksInfo.some((b) => b.url || b.goal);
}

const TITLE_GENERATION_DEBOUNCE_MS = 4000;

function useAutoGenerateWorkflowTitle(nodes: AppNode[], edges: Edge[]): void {
  const credentialGetter = useCredentialGetter();
  const abortControllerRef = useRef<AbortController | null>(null);

  // Derive a stable content fingerprint so we only react to actual block
  // content changes, not to layout/dimension/position updates on nodes.
  const contentFingerprint = useMemo(() => {
    const blocks = getWorkflowBlocks(nodes, edges);
    const info = blocks.slice(0, 5).map(extractBlockInfo);
    return JSON.stringify(info);
  }, [nodes, edges]);

  // useDebouncedCallback returns a stable reference (uses useMemo internally
  // with static deps), so it's safe to call in effects without listing it as
  // a dependency.
  const debouncedGenerate = useDebouncedCallback(
    async (blocksInfo: BlockInfo[]) => {
      // Re-check title state right before making the API call
      const state = useWorkflowTitleStore.getState();
      if (!state.isNewTitle() || state.titleHasBeenGenerated) {
        return;
      }

      // Cancel any previous in-flight request
      abortControllerRef.current?.abort();
      const controller = new AbortController();
      abortControllerRef.current = controller;

      try {
        const client = await getClient(credentialGetter, "sans-api-v1");
        const response = await client.post<
          { blocks: BlockInfo[] },
          { data: { title: string | null } }
        >(
          "/prompts/generate-workflow-title",
          { blocks: blocksInfo },
          { signal: controller.signal },
        );

        // Re-check after async call - user may have edited title during the request
        const currentState = useWorkflowTitleStore.getState();
        if (
          currentState.isNewTitle() &&
          !currentState.titleHasBeenGenerated &&
          response.data.title
        ) {
          currentState.setTitleFromGeneration(response.data.title);
        }
      } catch {
        // Silently ignore - abort errors, network errors, etc.
        // The first-save fallback in create_workflow_from_request still works.
      }
    },
    TITLE_GENERATION_DEBOUNCE_MS,
  );

  useEffect(() => {
    const state = useWorkflowTitleStore.getState();

    // Only auto-generate for new, untouched workflows
    if (!state.isNewTitle() || state.titleHasBeenGenerated) {
      debouncedGenerate.cancel();
      return;
    }

    const blocksInfo: BlockInfo[] = JSON.parse(contentFingerprint);

    if (!hasMeaningfulContent(blocksInfo)) {
      debouncedGenerate.cancel();
      return;
    }

    debouncedGenerate(blocksInfo);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [contentFingerprint]);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      debouncedGenerate.cancel();
      abortControllerRef.current?.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}

export { useAutoGenerateWorkflowTitle };
