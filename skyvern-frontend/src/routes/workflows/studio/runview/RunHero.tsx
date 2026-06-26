import { type ReactNode, useEffect, useState } from "react";
import {
  CodeIcon,
  CounterClockwiseClockIcon,
  ExclamationTriangleIcon,
  GlobeIcon,
  PlayIcon,
} from "@radix-ui/react-icons";

import { Button } from "@/components/ui/button";
import { StreamStatusPanel } from "@/routes/streaming/StreamDiagnostics";
import { useRunViewStore } from "@/store/RunViewStore";
import { cn } from "@/util/utils";

import { WorkflowRunCode } from "../../workflowRun/WorkflowRunCode";
import { FilmstripFrame } from "../runProjections";
import { HeroRecording } from "./HeroRecording";
import { HeroScreenshot } from "./HeroScreenshot";
import { RunLiveStream } from "./RunLiveStream";

type RunHeroProps = {
  workflowRunId: string;
  shownFrame: FilmstripFrame | null;
  running: boolean;
  provisioning: boolean;
  isPaused: boolean;
  failed: boolean;
  failureReason: string | null;
  browserSessionId: string | null;
  recordingUrls: string[];
  elapsed: string;
  details?: ReactNode;
  inputs?: ReactNode;
  outputs?: ReactNode;
  actions?: ReactNode;
  onRecordingPlay?: (index: number) => void;
  onFix?: () => void;
  onRetry?: () => void;
};

type CenterView = "code" | "stream" | "recording" | "screenshot";

function ViewToggle({
  active,
  onClick,
  icon,
  children,
}: {
  active: boolean;
  onClick: () => void;
  icon?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "inline-flex items-center gap-1.5 rounded px-2 py-1 text-[11px] font-medium",
        "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
        active
          ? "bg-studio-accent/15 text-studio-accent-2"
          : "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
      )}
    >
      {icon}
      {children}
    </button>
  );
}

export function RunHero({
  workflowRunId,
  shownFrame,
  running,
  provisioning,
  isPaused,
  failed,
  failureReason,
  browserSessionId,
  recordingUrls,
  elapsed,
  details,
  inputs,
  outputs,
  actions,
  onRecordingPlay,
  onFix,
  onRetry,
}: RunHeroProps) {
  const pinnedFrameId = useRunViewStore((s) => s.pinnedFrameId);
  const pinFrame = useRunViewStore((s) => s.pinFrame);
  const jumpToLive = useRunViewStore((s) => s.jumpToLive);
  const codeOpen = useRunViewStore((s) => s.codeOpen);
  const setCodeOpen = useRunViewStore((s) => s.setCodeOpen);

  // The live page URL comes from the stream frames (CDP); reset per run.
  const [streamUrl, setStreamUrl] = useState<string | null>(null);
  useEffect(() => {
    setStreamUrl(null);
  }, [workflowRunId]);

  const scrubbing = pinnedFrameId != null && pinnedFrameId !== "stream";
  const hasRecording = recordingUrls.length > 0;

  // A failed run defaults to its last screenshot so the fix/retry CTA is
  // visible; otherwise a finished run defaults to the recording.
  const center: CenterView = codeOpen
    ? "code"
    : running && !scrubbing
      ? "stream"
      : scrubbing
        ? "screenshot"
        : hasRecording && !failed
          ? "recording"
          : "screenshot";

  const headerLabel =
    center === "stream"
      ? // VNC streams carry no page URL (pixels only); only the CDP path sets
        // streamUrl, so fall back to a neutral label instead of "Loading…".
        (streamUrl ?? "Live browser")
      : center === "recording"
        ? "Recording"
        : center === "code"
          ? "Generated code"
          : (shownFrame?.label ?? "Screenshot");

  const headerIcon =
    center === "recording" ? (
      <PlayIcon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
    ) : center === "code" ? (
      <CodeIcon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
    ) : (
      <GlobeIcon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
    );

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-lg border border-border bg-slate-elevation1">
      <div className="flex items-center gap-2 border-b border-border px-3 py-2">
        <div className="flex min-w-0 flex-1 items-center gap-1.5">
          {headerIcon}
          <span className="min-w-0 flex-1 truncate text-sm font-medium text-foreground">
            {headerLabel}
          </span>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {running ? (
            <ViewToggle
              active={center === "stream"}
              onClick={() => pinFrame("stream")}
              icon={
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-success" />
              }
            >
              Live
            </ViewToggle>
          ) : hasRecording ? (
            <ViewToggle
              active={center === "recording"}
              onClick={jumpToLive}
              icon={<PlayIcon className="h-3 w-3" />}
            >
              Recording
            </ViewToggle>
          ) : null}
          <ViewToggle
            active={center === "code"}
            onClick={() => setCodeOpen(!codeOpen)}
            icon={<CodeIcon className="h-3 w-3" />}
          >
            Code
          </ViewToggle>
          {details ? (
            <>
              <div className="mx-0.5 h-4 w-px bg-border" />
              {details}
            </>
          ) : null}
          {inputs ? (
            <>
              <div className="mx-0.5 h-4 w-px bg-border" />
              {inputs}
            </>
          ) : null}
          {outputs ? (
            <>
              <div className="mx-0.5 h-4 w-px bg-border" />
              {outputs}
            </>
          ) : null}
          {actions ? (
            <>
              <div className="mx-0.5 h-4 w-px bg-border" />
              {actions}
            </>
          ) : null}
          <span
            className="ml-1 whitespace-nowrap font-mono text-[11px] tabular-nums text-muted-foreground"
            title="Elapsed"
          >
            {elapsed}
          </span>
        </div>
      </div>

      <div className="relative min-h-0 flex-1 overflow-hidden bg-slate-950">
        {center === "code" ? (
          <div className="absolute inset-0 flex flex-col overflow-hidden bg-slate-elevation1 p-2">
            <WorkflowRunCode
              workflowRunId={workflowRunId}
              showCacheKeyValueSelector
            />
          </div>
        ) : center === "stream" ? (
          provisioning ? (
            // Mounting the stream while the run is still queued opens a socket
            // the backend never feeds; wait until the run is actually running.
            <div className="absolute inset-0">
              <StreamStatusPanel
                diagnostic={{
                  title: "Starting the browser",
                  detail: "Getting your run's browser ready…",
                  pending: true,
                }}
              />
            </div>
          ) : (
            <RunLiveStream
              workflowRunId={workflowRunId}
              browserSessionId={browserSessionId}
              interactive={isPaused}
              onUrlChange={setStreamUrl}
            />
          )
        ) : center === "recording" ? (
          <HeroRecording
            recordingUrls={recordingUrls}
            onPlay={onRecordingPlay}
          />
        ) : shownFrame ? (
          <HeroScreenshot artifactId={shownFrame.screenshotArtifactId} />
        ) : (
          <div className="absolute inset-0 grid place-items-center text-sm text-muted-foreground">
            Waiting for the first action…
          </div>
        )}

        {center === "screenshot" && scrubbing && shownFrame ? (
          <div className="absolute left-3 top-3 flex max-w-[26rem] items-center gap-2 rounded-md bg-black/70 px-3 py-1.5 text-xs text-white backdrop-blur">
            <CounterClockwiseClockIcon className="h-3.5 w-3.5 shrink-0" />
            <span className="truncate">
              Inspecting step · <b>{shownFrame.label}</b>
            </span>
            {running ? (
              <button
                type="button"
                onClick={() => pinFrame("stream")}
                className="ml-1 inline-flex items-center gap-1.5 rounded-full bg-white/15 px-2 py-0.5 text-[11px] hover:bg-white/25 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-white"
              >
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-success" />
                Jump to live
              </button>
            ) : hasRecording ? (
              <button
                type="button"
                onClick={jumpToLive}
                className="ml-1 inline-flex items-center gap-1.5 rounded-full bg-white/15 px-2 py-0.5 text-[11px] hover:bg-white/25 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-white"
              >
                <PlayIcon className="h-3 w-3" />
                Recording
              </button>
            ) : null}
          </div>
        ) : null}

        {center === "screenshot" && failed && !scrubbing ? (
          <div className="absolute inset-x-0 bottom-0 m-4 rounded-lg border border-destructive/40 bg-slate-elevation1/95 p-4 shadow-lg backdrop-blur">
            <div className="flex items-center gap-2 text-sm font-semibold text-foreground">
              <ExclamationTriangleIcon className="h-4 w-4 text-destructive" />
              {failureReason ?? "The run failed."}
            </div>
            <div className="mt-3 flex flex-wrap gap-2">
              {onFix ? (
                <Button
                  size="sm"
                  className="bg-studio-accent text-studio-accent-foreground hover:bg-studio-accent/90"
                  onClick={onFix}
                >
                  Fix with Copilot
                </Button>
              ) : null}
              {onRetry ? (
                <Button size="sm" variant="secondary" onClick={onRetry}>
                  Retry as-is
                </Button>
              ) : null}
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}
