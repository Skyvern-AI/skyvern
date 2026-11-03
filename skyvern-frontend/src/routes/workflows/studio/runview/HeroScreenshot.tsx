import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { ReloadIcon } from "@radix-ui/react-icons";

import { useArtifactImageSrc } from "@/hooks/useArtifactImageSrc";
import { getImageURL } from "@/routes/tasks/detail/artifactUtils";

import { screenshotZoomClasses } from "./HeroScreenshot.utils";
import { useHeroScreenshot } from "./useHeroScreenshot";

const PRIMARY_RETRY_MS = 5000;

export type HeroSelection =
  | {
      kind: "action";
      artifactId: string | null;
      stepId: string | null;
      actionOrder: number | null;
      workflowRunBlockId: string | null;
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
 * long captures.
 */
export function HeroScreenshot({
  selection,
  running,
  unavailableText = "Screenshot unavailable.",
}: {
  selection: HeroSelection | null;
  running: boolean;
  unavailableText?: string;
}) {
  const [zoomed, setZoomed] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const {
    screenshot: primary,
    fallbackScreenshot,
    isLoading,
  } = useHeroScreenshot(selection, running);
  const [failedPrimaryId, setFailedPrimaryId] = useState<string | null>(null);
  const retriedPrimaryIds = useRef(new Set<string>());
  const primaryId = primary?.artifact_id ?? null;
  const canFallBack =
    fallbackScreenshot !== undefined &&
    fallbackScreenshot.artifact_id !== primary?.artifact_id;
  const latched = canFallBack && failedPrimaryId === primaryId;
  const loaded = latched ? fallbackScreenshot : primary;
  const { src, onImageError, imageFailed } = useArtifactImageSrc(loaded);
  // An at-failure capture can be listed before its upload finishes; show the prior action shot in the same render.
  const primaryFailed = imageFailed && canFallBack && !latched;
  const screenshot = primaryFailed ? fallbackScreenshot : loaded;

  useEffect(() => {
    if (primaryFailed && primaryId) {
      setFailedPrimaryId(primaryId);
    }
  }, [primaryFailed, primaryId]);

  // The upload may still finish, so give a latched primary one more chance.
  useEffect(() => {
    if (
      !failedPrimaryId ||
      failedPrimaryId !== primaryId ||
      retriedPrimaryIds.current.has(failedPrimaryId)
    ) {
      return;
    }
    const timer = setTimeout(() => {
      retriedPrimaryIds.current.add(failedPrimaryId);
      setFailedPrimaryId(null);
    }, PRIMARY_RETRY_MS);
    return () => clearTimeout(timer);
  }, [failedPrimaryId, primaryId]);

  useEffect(() => {
    setZoomed(false);
  }, [primaryId]);

  // Start each view at the top; when zoomed, center horizontally too (margin-auto
  // resolves to 0 once the image overflows, so the scroll would default to left).
  useLayoutEffect(() => {
    const el = containerRef.current;
    if (!el) {
      return;
    }
    el.scrollTop = 0;
    el.scrollLeft = zoomed ? (el.scrollWidth - el.clientWidth) / 2 : 0;
  }, [zoomed, primaryId]);

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
  if (!screenshot || screenshot.archived || (imageFailed && !primaryFailed)) {
    return (
      <div className="absolute inset-0 grid place-items-center text-sm text-muted-foreground">
        {!screenshot ? unavailableText : "Screenshot unavailable."}
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
        src={primaryFailed ? getImageURL(screenshot) : src}
        alt="screenshot"
        className={zoom.image}
        onError={onImageError}
      />
    </div>
  );
}
