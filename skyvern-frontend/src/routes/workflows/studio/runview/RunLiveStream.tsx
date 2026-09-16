import { StreamStatusPanel } from "@/routes/streaming/StreamDiagnostics";
import type { Status, WorkflowRunRetryFields } from "@/api/types";
import {
  getRunAttemptKey,
  runIsRetryWaiting,
} from "../../workflowRun/runRetryState";
import { useEffect, useState } from "react";

import { BrowserStream } from "@/components/BrowserStream";
import { useStreamTransport } from "@/hooks/useRuntimeConfig";
import { BrowserSessionStream } from "@/routes/browserSessions/BrowserSessionStream";

import { WorkflowRunStream } from "../../workflowRun/WorkflowRunStream";

type RunLiveStreamProps = {
  workflowRunId: string;
  run?: { workflow_run_id: string; status: Status } & WorkflowRunRetryFields;
  browserSessionId: string | null;
  interactive: boolean;
  // Live page URL from the CDP frames; the VNC path doesn't surface one yet.
  onUrlChange?: (url: string) => void;
};

/**
 * Live browser for a workflow run, mirroring WorkflowRunOverview: VNC keyed by the
 * browser session, with the session's CDP screencast when VNC is wrong or closes early.
 */
export function RunLiveStream(props: RunLiveStreamProps) {
  if (props.run && runIsRetryWaiting(props.run)) {
    return (
      <StreamStatusPanel
        diagnostic={{
          title: "Retry pending",
          detail: "The browser reconnects when the next attempt starts.",
          pending: true,
        }}
      />
    );
  }
  return (
    <RunLiveStreamTransport
      key={props.run ? getRunAttemptKey(props.run) : props.workflowRunId}
      {...props}
    />
  );
}

function RunLiveStreamTransport({
  workflowRunId,
  browserSessionId,
  interactive,
  onUrlChange,
}: RunLiveStreamProps) {
  const { streamTransport } = useStreamTransport(browserSessionId);
  const [vncFailed, setVncFailed] = useState(false);

  useEffect(() => {
    setVncFailed(false);
  }, [browserSessionId]);

  if (browserSessionId) {
    if (!streamTransport) {
      // Defaulting to VNC here opens a socket an externally hosted session cannot serve, and the
      // failure is what swaps the transport — a visible drop rather than a first paint.
      return null;
    }

    if (streamTransport !== "cdp" && !vncFailed) {
      return (
        <BrowserStream
          key={browserSessionId}
          browserSessionId={browserSessionId}
          interactive={interactive}
          showControlButtons={interactive}
          // A recording can be live while this per-run stream mounts and unmounts;
          // StudioBrowserStream owns the session-level reset.
          resetRecordingOnUnmount={false}
          onClose={() => setVncFailed(true)}
        />
      );
    }

    // The per-run stream is fed only by display capture on the worker, which stops as soon as a
    // run has a browser_session_id — so a session-backed run must stream the session itself.
    return (
      <BrowserSessionStream
        key={browserSessionId}
        browserSessionId={browserSessionId}
        interactive={interactive}
        showControlButtons={interactive}
        onUrlChange={onUrlChange}
        centered
      />
    );
  }

  return (
    <WorkflowRunStream
      workflowRunId={workflowRunId}
      alwaysShowStream
      interactive={interactive}
      showControlButtons={interactive}
      onUrlChange={onUrlChange}
      centered
    />
  );
}
