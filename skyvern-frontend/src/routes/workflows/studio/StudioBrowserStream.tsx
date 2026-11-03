import { useCallback, useEffect } from "react";

import { useStudioBrowserStore } from "@/store/useStudioBrowserStore";

import { StreamPresenter } from "./StreamPresenter";
import { type StudioPaneId } from "./panes";
import { useBrowserPaneView } from "./useBrowserPaneView";
import { useExecutingBlockRun } from "./useExecutingBlockRun";
import { useStudioPanes } from "./useStudioPanes";

/**
 * The studio's single live-browser stream, portaled into a host node re-parented
 * between the open panes so the socket persists instead of re-booting.
 */
export function StudioBrowserStream({
  visiblePanes,
}: {
  visiblePanes?: readonly StudioPaneId[];
}) {
  const { panes } = useStudioPanes();
  const renderedPanes = visiblePanes ?? panes;
  const browserPaneOpen = renderedPanes.includes("browser");
  const reloadNonce = useStudioBrowserStore((s) => s.reloadNonce);
  const setStreamUrl = useStudioBrowserStore((s) => s.setStreamUrl);
  const setDebugStreamState = useStudioBrowserStore(
    (s) => s.setDebugStreamState,
  );
  const markActivity = useStudioBrowserStore((s) => s.markActivity);
  const clearActivity = useStudioBrowserStore((s) => s.clearActivity);
  const reset = useStudioBrowserStore((s) => s.reset);
  const { view, liveSurface, debugBrowserSessionId } = useBrowserPaneView();
  const browserSessionId = debugBrowserSessionId;
  const executingBlockRun = useExecutingBlockRun();
  // Co-drive: take-control stays available while a block run executes; the pill
  // just flags the shared browser.
  const coDriving = executingBlockRun;
  // Only offer control while this stream is the pane's visible surface. A replay
  // view or a per-run stream parks this node; withdrawing the offer makes
  // BrowserStream release any held grab (it can't be exercised unseen).
  const debugStreamShown =
    browserPaneOpen && view === "live" && liveSurface === "debug";

  useEffect(() => {
    reset();
    return () => reset();
  }, [browserSessionId, reset]);

  useEffect(() => {
    if (browserPaneOpen) {
      clearActivity();
    }
  }, [clearActivity, browserPaneOpen]);

  const handleUrlChange = useCallback(
    (url: string) => {
      setStreamUrl(url);
    },
    [setStreamUrl],
  );

  const handleActivity = useCallback(() => {
    if (browserPaneOpen) {
      clearActivity();
      return;
    }
    markActivity();
  }, [clearActivity, markActivity, browserPaneOpen]);

  if (!browserSessionId) {
    return null;
  }

  return (
    <div className="relative h-full w-full">
      <StreamPresenter
        key={`${browserSessionId}:${reloadNonce}`}
        browserSessionId={browserSessionId}
        interactive={false}
        showControlButtons={debugStreamShown}
        // The CDP transport streams the page viewport only, so unlike the VNC
        // panel there is no browser address bar to type into and a session
        // resting on about:blank has no way out (SKY-13705).
        enableUrlInput={true}
        onUrlChange={handleUrlChange}
        onStreamStateChange={setDebugStreamState}
        onActivity={handleActivity}
      />
      {coDriving && debugStreamShown ? (
        <div
          role="status"
          className="pointer-events-none absolute left-1/2 top-3 z-10 flex max-w-[90%] -translate-x-1/2 items-center gap-2 rounded-md bg-black/70 px-3 py-1.5 text-xs text-white backdrop-blur duration-200 motion-safe:animate-in motion-safe:fade-in"
        >
          <span
            aria-hidden
            className="size-1.5 shrink-0 rounded-full bg-success motion-safe:animate-pulse"
          />
          <span className="truncate">
            Agent is running — you're sharing the browser
          </span>
        </div>
      ) : null}
    </div>
  );
}
