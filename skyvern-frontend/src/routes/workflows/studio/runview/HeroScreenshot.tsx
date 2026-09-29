import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { ReloadIcon } from "@radix-ui/react-icons";

import { useArtifactImageSrc } from "@/hooks/useArtifactImageSrc";

import { screenshotZoomClasses } from "./HeroScreenshot.utils";
import { useHeroScreenshot } from "./useHeroScreenshot";

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
 * long captures.
 */
export function HeroScreenshot({
  selection,
  running,
}: {
  selection: HeroSelection | null;
  running: boolean;
}) {
  const [zoomed, setZoomed] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const { screenshot, isLoading } = useHeroScreenshot(selection, running);

  const screenshotId = screenshot?.artifact_id ?? null;
  const { src, onImageError, imageFailed } = useArtifactImageSrc(screenshot);

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
  if (!screenshot || screenshot.archived || imageFailed) {
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
        src={src}
        alt="screenshot"
        className={zoom.image}
        onError={onImageError}
      />
    </div>
  );
}
