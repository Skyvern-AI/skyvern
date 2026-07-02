import { useCallback, useEffect } from "react";
import { useParams } from "react-router-dom";

import { useRecordingStore } from "@/store/useRecordingStore";
import { useStudioBrowserStore } from "@/store/useStudioBrowserStore";

import { useDebugSessionQuery } from "../hooks/useDebugSessionQuery";
import { StreamPresenter } from "./StreamPresenter";
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
    <StreamPresenter
      key={`${browserSessionId}:${reloadNonce}`}
      browserSessionId={browserSessionId}
      interactive={false}
      showControlButtons={browserPaneOpen}
      isRecording={isRecording}
      onUrlChange={handleUrlChange}
      onActivity={handleActivity}
    />
  );
}
