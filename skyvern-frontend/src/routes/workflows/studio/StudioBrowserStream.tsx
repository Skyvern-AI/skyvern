import { useCallback, useEffect } from "react";
import { useParams } from "react-router-dom";

import { useRecordingStore } from "@/store/useRecordingStore";
import { useStudioBrowserStore } from "@/store/useStudioBrowserStore";
import { useStudioShellStore } from "@/store/StudioShellStore";

import { useDebugSessionQuery } from "../hooks/useDebugSessionQuery";
import { StreamPresenter } from "./StreamPresenter";

/**
 * The studio's single live-browser stream, portaled into a host node re-parented
 * between the PiP and Browser tab so the socket persists instead of re-booting.
 */
export function StudioBrowserStream() {
  const { workflowPermanentId } = useParams();
  const tab = useStudioShellStore((s) => s.tab);
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
    if (tab === "browser") {
      clearActivity();
    }
  }, [clearActivity, tab]);

  const handleUrlChange = useCallback(
    (url: string) => {
      setStreamUrl(url);
    },
    [setStreamUrl],
  );

  const handleActivity = useCallback(() => {
    if (tab === "browser") {
      clearActivity();
      return;
    }
    markActivity();
  }, [clearActivity, markActivity, tab]);

  if (!browserSessionId) {
    return null;
  }

  return (
    <StreamPresenter
      key={`${browserSessionId}:${reloadNonce}`}
      browserSessionId={browserSessionId}
      interactive={false}
      showControlButtons={tab === "browser"}
      isRecording={isRecording}
      onUrlChange={handleUrlChange}
      onActivity={handleActivity}
    />
  );
}
