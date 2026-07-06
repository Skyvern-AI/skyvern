import { useCallback } from "react";
import { ClockIcon, CounterClockwiseClockIcon } from "@radix-ui/react-icons";
import { usePostHog } from "posthog-js/react";

import { StreamStatusPanel } from "@/routes/streaming/StreamDiagnostics";

import { PasteRecordedStepsHint } from "@/routes/workflows/copilot/PasteRecordedStepsHint";
import { useRecordingStore } from "@/store/useRecordingStore";

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
  const isRecording = useRecordingStore((s) => s.isRecording);

  const {
    workflowRun,
    running,
    provisioning,
    isPaused,
    recordingUrls,
    heroSelection,
    heroLabel,
    scrubbing,
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
    <div className="flex h-full min-h-0 w-full flex-col gap-3 p-3">
      {!isRecording ? <PasteRecordedStepsHint /> : null}

      <div className="relative flex min-h-0 flex-1 items-center justify-center overflow-hidden rounded-lg border border-border bg-slate-950">
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
            <div className="absolute inset-0 grid place-items-center text-sm text-muted-foreground">
              No recording for this run
            </div>
          )
        ) : heroSelection ? (
          <>
            <HeroScreenshot selection={heroSelection} running={running} />
            {scrubbing ? (
              <div className="absolute left-3 top-3 flex max-w-[26rem] items-center gap-2 rounded-md bg-black/70 px-3 py-1.5 text-xs text-white backdrop-blur">
                <CounterClockwiseClockIcon className="h-3.5 w-3.5 shrink-0" />
                <span className="truncate">
                  Inspecting · <b>{heroLabel}</b>
                </span>
              </div>
            ) : null}
          </>
        ) : (
          <div className="absolute inset-0 grid place-items-center text-sm text-muted-foreground">
            {visuals.finalized
              ? "No screenshots for this run"
              : "Waiting for the first action…"}
          </div>
        )}
      </div>
    </div>
  );
}
