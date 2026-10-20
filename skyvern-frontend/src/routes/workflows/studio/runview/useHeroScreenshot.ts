import { useQuery } from "@tanstack/react-query";

import { getClient } from "@/api/AxiosClient";
import { ArtifactApiResponse, ArtifactType } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { apiPathPrefix } from "@/util/env";

import { selectBlockScreenshot } from "../../workflowRun/blockScreenshot";
import type { HeroSelection } from "./HeroScreenshot";

/**
 * Resolves a selection to its screenshot artifact: an action by artifact id, falling
 * back to the step's action screenshots; a block via `selectBlockScreenshot`; a
 * thought by its LLM screenshot.
 */
export function useHeroScreenshot(
  selection: HeroSelection | null,
  running: boolean,
): { screenshot: ArtifactApiResponse | undefined; isLoading: boolean } {
  const credentialGetter = useCredentialGetter();

  const action = selection?.kind === "action" ? selection : null;
  const block = selection?.kind === "block" ? selection : null;
  const thought = selection?.kind === "thought" ? selection : null;

  const { data: artifactById, isLoading: loadingArtifact } =
    useQuery<ArtifactApiResponse>({
      queryKey: ["artifact", action?.artifactId],
      queryFn: async () => {
        const client = await getClient(credentialGetter, "sans-api-v1");
        return client
          .get(`/artifacts/${action!.artifactId}`)
          .then((response) => response.data);
      },
      enabled: Boolean(action?.artifactId),
      refetchOnWindowFocus: false,
      staleTime: Infinity,
      retry: 1,
    });

  // Fallback path only when the action carries no explicit screenshot id: the
  // step's action screenshots, indexed by action order (newest-first), like legacy.
  const useStepFallback =
    Boolean(action?.stepId) &&
    action?.actionOrder != null &&
    !action?.artifactId;
  const { data: stepArtifacts, isLoading: loadingStep } = useQuery<
    Array<ArtifactApiResponse>
  >({
    queryKey: ["step", action?.stepId, "artifacts"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .get(`${apiPathPrefix}/step/${action!.stepId}/artifacts`)
        .then((response) => response.data);
    },
    enabled: useStepFallback,
    refetchInterval: running ? 5000 : false,
    refetchOnWindowFocus: false,
    staleTime: running ? 0 : Infinity,
    retry: 1,
  });

  const { data: blockArtifacts, isLoading: loadingBlock } = useQuery<
    Array<ArtifactApiResponse>
  >({
    queryKey: ["workflowRunBlock", block?.workflowRunBlockId, "artifacts"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .get(
          `${apiPathPrefix}/workflow_run_block/${block!.workflowRunBlockId}/artifacts`,
        )
        .then((response) => response.data);
    },
    enabled: Boolean(block?.workflowRunBlockId),
    refetchInterval: running ? 5000 : false,
    refetchOnWindowFocus: false,
    // Artifacts are immutable once the run finishes; only keep polling while live.
    staleTime: running ? 0 : Infinity,
    retry: 1,
  });

  const { data: thoughtArtifacts, isLoading: loadingThought } = useQuery<
    Array<ArtifactApiResponse>
  >({
    queryKey: ["observerThought", thought?.thoughtId, "artifacts"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .get(`${apiPathPrefix}/thought/${thought!.thoughtId}/artifacts`)
        .then((response) => response.data);
    },
    enabled: Boolean(thought?.thoughtId),
    refetchInterval: running ? 5000 : false,
    refetchOnWindowFocus: false,
    staleTime: running ? 0 : Infinity,
    retry: 1,
  });

  let screenshot: ArtifactApiResponse | undefined;
  if (action) {
    const actionShots = stepArtifacts?.filter(
      (artifact) => artifact.artifact_type === ArtifactType.ActionScreenshot,
    );
    const fromStep =
      actionShots && action.actionOrder != null
        ? actionShots[actionShots.length - action.actionOrder - 1]
        : undefined;
    screenshot = artifactById ?? fromStep;
  } else if (block) {
    screenshot = selectBlockScreenshot(
      blockArtifacts,
      block.blockType ?? undefined,
    );
  } else if (thought) {
    const thoughtShots = thoughtArtifacts?.filter(
      (artifact) => artifact.artifact_type === ArtifactType.LLMScreenshot,
    );
    // Thought LLM screenshots arrive newest-first; the last is the capture.
    screenshot = thoughtShots?.[thoughtShots.length - 1];
  }

  return {
    screenshot,
    isLoading: loadingArtifact || loadingStep || loadingBlock || loadingThought,
  };
}
