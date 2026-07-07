import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { ReloadIcon } from "@radix-ui/react-icons";
import { useQuery } from "@tanstack/react-query";

import { getClient } from "@/api/AxiosClient";
import { ArtifactApiResponse, ArtifactType } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { getImageURL } from "@/routes/tasks/detail/artifactUtils";
import { apiPathPrefix } from "@/util/env";

import { selectBlockScreenshot } from "../../workflowRun/blockScreenshot";
import { screenshotZoomClasses } from "./HeroScreenshot.utils";

export type HeroSelection =
  | {
      kind: "action";
      artifactId: string | null;
      stepId: string | null;
      actionOrder: number | null;
    }
  | {
      kind: "block";
      workflowRunBlockId: string;
      blockType: string | null;
    }
  | {
      kind: "thought";
      thoughtId: string;
    };

/**
 * The selected element's screenshot, fit to the run-hero width and scrollable for
 * long captures. Mirrors the legacy run page: an action shows its own post-action
 * screenshot (by artifact id, falling back to the step's action screenshots), a
 * block shows its representative screenshot via `selectBlockScreenshot`.
 */
export function HeroScreenshot({
  selection,
  running,
}: {
  selection: HeroSelection | null;
  running: boolean;
}) {
  const credentialGetter = useCredentialGetter();
  const [zoomed, setZoomed] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

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

  const screenshotId = screenshot?.artifact_id ?? null;
  const isLoading =
    loadingArtifact || loadingStep || loadingBlock || loadingThought;

  useEffect(() => {
    setZoomed(false);
  }, [screenshotId]);

  // Start each view at the top; when zoomed, center horizontally too (margin-auto
  // resolves to 0 once the image overflows, so the scroll would default to left).
  useLayoutEffect(() => {
    const el = containerRef.current;
    if (!el) {
      return;
    }
    el.scrollTop = 0;
    el.scrollLeft = zoomed ? (el.scrollWidth - el.clientWidth) / 2 : 0;
  }, [zoomed, screenshotId]);

  if (!selection) {
    return (
      <div className="absolute inset-0 grid place-items-center text-sm text-muted-foreground">
        No screenshot for this selection.
      </div>
    );
  }
  // Spinner only on the first load, not on poll refetches (which keep prior data).
  if (isLoading && !screenshot) {
    return (
      <div className="absolute inset-0 flex items-center justify-center gap-2 text-sm text-muted-foreground">
        <ReloadIcon className="h-5 w-5 animate-spin" /> Loading screenshot…
      </div>
    );
  }
  if (!screenshot || screenshot.archived) {
    return (
      <div className="absolute inset-0 grid place-items-center text-sm text-muted-foreground">
        Screenshot unavailable.
      </div>
    );
  }

  const toggleZoom = () => setZoomed((z) => !z);
  const zoom = screenshotZoomClasses(zoomed);
  return (
    <div
      ref={containerRef}
      className={zoom.container}
      role="button"
      tabIndex={0}
      aria-label={zoomed ? "Zoom screenshot out" : "Zoom screenshot in"}
      onClick={toggleZoom}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          toggleZoom();
        }
      }}
    >
      <img
        src={getImageURL(screenshot)}
        alt="screenshot"
        className={zoom.image}
      />
    </div>
  );
}
