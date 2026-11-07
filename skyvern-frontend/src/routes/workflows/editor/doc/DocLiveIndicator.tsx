import { useStudioBrowserStore } from "@/store/useStudioBrowserStore";
import { browserIsLive } from "../../studio/browserLiveStatus";
import { useBrowserPaneView } from "../../studio/useBrowserPaneView";

/** Shown whenever the Studio Browser pane has a confirmed live browser stream. */
export function DocLiveIndicator() {
  const { liveSurface, debugBrowserSessionId, runId } = useBrowserPaneView();
  const debugStream = useStudioBrowserStore((s) => s.debugStream);
  const runStream = useStudioBrowserStore((s) => s.runStream);
  if (
    !browserIsLive({
      liveSurface,
      debugBrowserSessionId,
      debugStream,
      runId,
      runStream,
    })
  )
    return null;
  return (
    <span
      role="status"
      className="inline-flex items-center gap-1.5 text-[12.5px] text-muted-foreground"
    >
      <span
        aria-hidden
        className="size-1.5 rounded-full bg-success motion-safe:animate-pulse"
      />
      Live
    </span>
  );
}
