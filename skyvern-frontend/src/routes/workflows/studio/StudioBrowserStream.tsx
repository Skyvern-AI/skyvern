import { useCallback, useEffect } from "react";
import { useParams } from "react-router-dom";

import { useRecordingStore } from "@/store/useRecordingStore";
import { useStudioBrowserStore } from "@/store/useStudioBrowserStore";

import { useDebugSessionQuery } from "../hooks/useDebugSessionQuery";
import { StreamPresenter } from "./StreamPresenter";
import { useExecutingBlockRun } from "./useExecutingBlockRun";
import { useStudioPanes } from "./useStudioPanes";

/**
 * The studio's single live-browser stream, portaled into a host node re-parented
 * between the open panes so the socket persists instead of re-booting.
 */
export function StudioBrowserStream() {
  const { workflowPermanentId } = useParams();
  const { panes } = useStudioPanes();
  const browserPaneOpen = panes.includes("browser");
  const isRecording = useRecordingStore((s) => s.isRecording);
  const reloadNonce = useStudioBrowserStore((s) => s.reloadNonce);
  const setStreamUrl = useStudioBrowserStore((s) => s.setStreamUrl);
  const markActivity = useStudioBrowserStore((s) => s.markActivity);
  const clearActivity = useStudioBrowserStore((s) => s.clearActivity);
  const reset = useStudioBrowserStore((s) => s.reset);
  const { data: debugSession } = useDebugSessionQuery({
    workflowPermanentId,
    enabled: false,
  });
  const browserSessionId = debugSession?.browser_session_id ?? null;
  const executingBlockRun = useExecutingBlockRun();
  // Recording keeps control by design — the recorder is driving the browser.
  const controlLocked = executingBlockRun && !isRecording;

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
        showControlButtons={browserPaneOpen && !controlLocked}
        isRecording={isRecording}
        onUrlChange={handleUrlChange}
        onActivity={handleActivity}
      />
      {/* Explains the missing take-control; only the Browser pane offers it. */}
      {controlLocked && browserPaneOpen ? (
        <div
          role="status"
          className="pointer-events-none absolute left-1/2 top-3 z-10 flex max-w-[90%] -translate-x-1/2 items-center gap-2 rounded-md bg-black/70 px-3 py-1.5 text-xs text-white backdrop-blur duration-200 motion-safe:animate-in motion-safe:fade-in"
        >
          <span
            aria-hidden
            className="size-1.5 shrink-0 rounded-full bg-studio-accent motion-safe:animate-pulse"
          />
          <span className="truncate">
            Skyvern is running this block — view only until it finishes
          </span>
        </div>
      ) : null}
    </div>
  );
}
