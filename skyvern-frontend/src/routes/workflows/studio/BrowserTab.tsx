import { useCallback } from "react";
import { ClockIcon } from "@radix-ui/react-icons";
import { usePostHog } from "posthog-js/react";

import { StreamStatusPanel } from "@/routes/streaming/StreamDiagnostics";

import { HeroRecording } from "./runview/HeroRecording";
import { HeroScreenshot } from "./runview/HeroScreenshot";
import { RunLiveStream } from "./runview/RunLiveStream";
import { useBrowserPaneView } from "./useBrowserPaneView";
import { useStudioShellContext } from "./StudioShellContext";

/**
 * Browser pane body of the studio shell — the run-aware visual surface. Live
 * shows the persistent debug browser (the shell re-parents the singleton stream
 * into this pane's slot) or the inspected run's own live stream; Recording and
 * Screenshots replay the inspected run. The view machine lives in
 * useBrowserPaneView; the pane chrome header hosts the pills and stream controls.
 */
export function BrowserTab() {
  const { setBrowserStreamSlot } = useStudioShellContext();
  const {
    view,
    visuals,
    runId,
    debugBrowserSessionId,
    runInDebugSession,
    liveSurface,
  } = useBrowserPaneView();
  const postHog = usePostHog();

  const {
    workflowRun,
    running,
    provisioning,
    isPaused,
    recordingUrls,
    heroSelection,
  } = visuals;

  const onRecordingPlay = useCallback(
    (index: number) => {
      if (!workflowRun) {
        return;
      }
      postHog.capture("run.recording.viewed", {
        org_id: workflowRun.workflow?.organization_id,
        run_id: workflowRun.workflow_run_id,
        recording_index: index,
        recording_count: recordingUrls.length,
      });
    },
    [postHog, workflowRun, recordingUrls.length],
  );

  // A running run outside the debug session streams through its own per-run
  // socket; everything else lives on the shared debug-session singleton.
  // (The runId check re-narrows for TS; "run" already implies it.)
  const showRunStream = liveSurface === "run" && runId != null;

  return (
    <div className="relative flex h-full min-h-0 w-full items-center justify-center overflow-hidden bg-slate-950">
      {view === "live" ? (
        showRunStream ? (
          provisioning ? (
            // Mounting the stream while the run is still queued opens a
            // socket the backend never feeds; wait until it actually runs.
            <StreamStatusPanel
              diagnostic={{
                title: "Starting the browser",
                detail: "Getting your run's browser ready…",
                pending: true,
              }}
            />
          ) : (
            <RunLiveStream
              workflowRunId={runId}
              browserSessionId={workflowRun?.browser_session_id ?? null}
              interactive={isPaused}
            />
          )
        ) : debugBrowserSessionId ? (
          <>
            <div
              ref={setBrowserStreamSlot}
              data-testid="browser-pane-stream-slot"
              className="absolute inset-0"
            />
            {provisioning && runInDebugSession ? (
              // A block run can queue behind a running full run
              // (run_sequentially); the debug browser is already live, so
              // say why nothing moves yet.
              <div className="absolute left-3 top-3 flex items-center gap-2 rounded-md bg-black/70 px-3 py-1.5 text-xs text-white backdrop-blur">
                <ClockIcon className="h-3.5 w-3.5 shrink-0" />
                <span>Run queued — waiting to start</span>
              </div>
            ) : null}
          </>
        ) : (
          <StreamStatusPanel
            diagnostic={{
              title: "Warming up your browser",
              detail:
                "Spinning up the debug browser — this only takes a moment.",
              pending: true,
            }}
          />
        )
      ) : view === "recording" ? (
        recordingUrls.length > 0 ? (
          <HeroRecording
            recordingUrls={recordingUrls}
            onPlay={onRecordingPlay}
          />
        ) : (
          <StreamStatusPanel
            diagnostic={{
              title: "No recording for this run",
              detail:
                "Screenshots keep a frame for each action the run took — try that view instead.",
            }}
          />
        )
      ) : heroSelection ? (
        <HeroScreenshot selection={heroSelection} running={running} />
      ) : visuals.finalized ? (
        <StreamStatusPanel
          diagnostic={{
            title: "No screenshots for this run",
            detail: "The run finished without capturing any.",
          }}
        />
      ) : (
        // Nothing has been captured *yet* — the run is still going. Rendered as
        // a bare sentence this was indistinguishable from the finalized empty
        // state above, so a live run read as a dead end.
        <StreamStatusPanel
          diagnostic={{
            title: "Waiting for the first action",
            detail: "Screenshots appear here as the run takes them.",
            pending: true,
          }}
        />
      )}
    </div>
  );
}
