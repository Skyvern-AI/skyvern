import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { ReloadIcon } from "@radix-ui/react-icons";
import { useQuery } from "@tanstack/react-query";

import { getClient } from "@/api/AxiosClient";
import { ArtifactApiResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { getImageURL } from "@/routes/tasks/detail/artifactUtils";
import { apiPathPrefix } from "@/util/env";

import { selectBlockScreenshot } from "../../workflowRun/blockScreenshot";
import { screenshotZoomClasses } from "./HeroScreenshot.utils";

/**
 * The active block's screenshot, fit to the run-hero width and scrollable for long
 * captures. Prefers the full-page LLM screenshot (what the agent saw) over the
 * per-action screenshot via `selectBlockScreenshot`, matching the legacy run page.
 */
export function HeroScreenshot({
  workflowRunBlockId,
  blockType,
  running,
}: {
  workflowRunBlockId: string | null;
  blockType: string | null;
  running: boolean;
}) {
  const credentialGetter = useCredentialGetter();
  const [zoomed, setZoomed] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  const { data: artifacts, isLoading } = useQuery<Array<ArtifactApiResponse>>({
    queryKey: ["workflowRunBlock", workflowRunBlockId, "artifacts"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .get(
          `${apiPathPrefix}/workflow_run_block/${workflowRunBlockId}/artifacts`,
        )
        .then((response) => response.data);
    },
    enabled: Boolean(workflowRunBlockId),
    refetchInterval: running ? 5000 : false,
    refetchOnWindowFocus: false,
    // Artifacts are immutable once the run finishes; only keep polling while live.
    staleTime: running ? 0 : Infinity,
    retry: 1,
  });

  const screenshot = selectBlockScreenshot(artifacts, blockType ?? undefined);
  const screenshotId = screenshot?.artifact_id ?? null;

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

  if (!workflowRunBlockId) {
    return (
      <div className="absolute inset-0 grid place-items-center text-sm text-muted-foreground">
        No screenshot for this action.
      </div>
    );
  }
  // Spinner only on the first load, not on poll refetches (which keep prior data).
  if (isLoading && !artifacts) {
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
        alt="block screenshot"
        className={zoom.image}
      />
    </div>
  );
}
