import { useQuery } from "@tanstack/react-query";

import { getClient } from "@/api/AxiosClient";
import { ArtifactApiResponse, ArtifactType } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { apiPathPrefix } from "@/util/env";
import { normalizeUtcTimestamp } from "@/util/timeFormat";

import { selectBlockScreenshot } from "../../workflowRun/blockScreenshot";
import type { HeroSelection } from "./HeroScreenshot";

const POST_RUN_FALLBACK_FETCHES = 6;

function createdAtMs(artifact: ArtifactApiResponse): number {
  return Date.parse(normalizeUtcTimestamp(artifact.created_at));
}

function newestActionShot(
  stepActionShots: Array<ArtifactApiResponse> | undefined,
  blockArtifacts: Array<ArtifactApiResponse> | undefined,
): ArtifactApiResponse | undefined {
  const fromStep = stepActionShots?.find((artifact) => !artifact.archived);
  const fromBlock = blockArtifacts?.find(
    (artifact) =>
      artifact.artifact_type === ArtifactType.ActionScreenshot &&
      !artifact.archived,
  );
  if (!fromStep || !fromBlock) {
    return fromStep ?? fromBlock;
  }
  // The step list can be a cache entry from before the run ended, so the block list may hold a newer shot.
  return createdAtMs(fromBlock) > createdAtMs(fromStep) ? fromBlock : fromStep;
}

function llmShots(
  blockArtifacts: Array<ArtifactApiResponse> | undefined,
): Array<ArtifactApiResponse> {
  return (blockArtifacts ?? []).filter(
    (artifact) =>
      artifact.artifact_type === ArtifactType.LLMScreenshot &&
      !artifact.archived,
  );
}

// Both lists arrive newest-first. A block-start LLM shot predates every action, so only one newer than the
// newest action shot is the at-failure capture.
function atFailureShot(
  stepActionShots: Array<ArtifactApiResponse> | undefined,
  blockArtifacts: Array<ArtifactApiResponse> | undefined,
): ArtifactApiResponse | undefined {
  const newestAction = newestActionShot(stepActionShots, blockArtifacts);
  if (!newestAction) {
    return undefined;
  }
  return llmShots(blockArtifacts).find(
    (artifact) => createdAtMs(artifact) > createdAtMs(newestAction),
  );
}

/**
 * On a step-index miss, an action falls back to its block's artifacts; the newest action shot is also returned as
 * fallbackScreenshot for a failed image load.
 */
export function useHeroScreenshot(
  selection: HeroSelection | null,
  running: boolean,
): {
  screenshot: ArtifactApiResponse | undefined;
  fallbackScreenshot: ArtifactApiResponse | undefined;
  isLoading: boolean;
} {
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

  const actionShots = stepArtifacts?.filter(
    (artifact) => artifact.artifact_type === ArtifactType.ActionScreenshot,
  );
  const fromStep =
    actionShots && action?.actionOrder != null
      ? actionShots[actionShots.length - action.actionOrder - 1]
      : undefined;
  // Any step-index miss, mainly a trailing failed row with no shot of its own; a live row whose shot has not
  // arrived yet also lands here.
  const stepMiss = useStepFallback && stepArtifacts != null && !fromStep;
  // Its own cache entry: the shared block key may hold a list fetched before the at-failure shot landed.
  const { data: fallbackArtifacts, isLoading: loadingFallback } = useQuery<
    Array<ArtifactApiResponse>
  >({
    queryKey: [
      "workflowRunBlock",
      action?.workflowRunBlockId,
      "artifacts",
      "heroFallback",
      running,
    ],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .get(
          `${apiPathPrefix}/workflow_run_block/${action!.workflowRunBlockId}/artifacts`,
        )
        .then((response) => response.data);
    },
    enabled: stepMiss && Boolean(action?.workflowRunBlockId),
    // The backend attaches the at-failure shot after the run turns terminal, so keep polling briefly past the end.
    refetchInterval: (query) => {
      if (running) {
        return 5000;
      }
      if (atFailureShot(actionShots, query.state.data)) {
        return false;
      }
      return query.state.dataUpdateCount + query.state.errorUpdateCount <
        POST_RUN_FALLBACK_FETCHES
        ? 5000
        : false;
    },
    placeholderData: (previous, previousQuery) =>
      previousQuery?.queryKey[1] === action?.workflowRunBlockId
        ? previous
        : undefined,
    refetchOnWindowFocus: false,
    staleTime: 0,
    retry: false,
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
  let fallbackScreenshot: ArtifactApiResponse | undefined;
  if (action) {
    screenshot = artifactById ?? fromStep;
    if (!screenshot && stepMiss) {
      fallbackScreenshot = newestActionShot(actionShots, fallbackArtifacts);
      screenshot =
        atFailureShot(actionShots, fallbackArtifacts) ??
        fallbackScreenshot ??
        llmShots(fallbackArtifacts)[0];
    }
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
    fallbackScreenshot,
    isLoading:
      loadingArtifact ||
      loadingStep ||
      loadingFallback ||
      loadingBlock ||
      loadingThought,
  };
}
