import { type ReactNode, useEffect, useRef, useState } from "react";
import {
  CodeIcon,
  CounterClockwiseClockIcon,
  Cross2Icon,
  ExclamationTriangleIcon,
  FileTextIcon,
  GlobeIcon,
  ListBulletIcon,
  PlayIcon,
} from "@radix-ui/react-icons";

import { Button } from "@/components/ui/button";
import { StreamStatusPanel } from "@/routes/streaming/StreamDiagnostics";
import { useRunViewStore } from "@/store/RunViewStore";
import { cn } from "@/util/utils";

import { WorkflowRunCode } from "../../workflowRun/WorkflowRunCode";
import { FilmstripFrame } from "../runProjections";
import { useStudioShellContext } from "../StudioShellContext";
import { HeroRecording } from "./HeroRecording";
import { HeroScreenshot } from "./HeroScreenshot";
import { RunLiveStream } from "./RunLiveStream";

type RunHeroProps = {
  workflowRunId: string;
  shownFrame: FilmstripFrame | null;
  running: boolean;
  // A block run shows the shared debug-session stream (re-parented in by the
  // shell), view-only, instead of mounting a separate run stream.
  showDebugStream: boolean;
  provisioning: boolean;
  isPaused: boolean;
  failed: boolean;
  failureReason: string | null;
  browserSessionId: string | null;
  recordingUrls: string[];
  elapsed: string;
  inputs?: ReactNode;
  outputs?: ReactNode;
  overview?: ReactNode;
  actions?: ReactNode;
  onRecordingPlay?: (index: number) => void;
  onFix?: () => void;
  onRetry?: () => void;
};

type CenterView =
  | "code"
  | "inputs"
  | "outputs"
  | "stream"
  | "recording"
  | "screenshot";

// Below this header width the toggle/dropdown labels collapse to icons.
const HEADER_COMPACT_BELOW_PX = 640;

function ViewToggle({
  active,
  onClick,
  icon,
  label,
  compact,
}: {
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  label: string;
  compact: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={compact ? label : undefined}
      aria-label={label}
      aria-pressed={active}
      className={cn(
        "inline-flex items-center gap-1.5 rounded px-2 py-1 text-[11px] font-medium",
        "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
        active
          ? "bg-studio-accent/15 text-foreground"
          : "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
      )}
    >
      {icon}
      {compact ? null : label}
    </button>
  );
}

export function RunHero({
  workflowRunId,
  shownFrame,
  running,
  showDebugStream,
  provisioning,
  isPaused,
  failed,
  failureReason,
  browserSessionId,
  recordingUrls,
  elapsed,
  inputs,
  outputs,
  overview,
  actions,
  onRecordingPlay,
  onFix,
  onRetry,
}: RunHeroProps) {
  const pinnedFrameId = useRunViewStore((s) => s.pinnedFrameId);
  const pinFrame = useRunViewStore((s) => s.pinFrame);
  const jumpToLive = useRunViewStore((s) => s.jumpToLive);
  const centerView = useRunViewStore((s) => s.centerView);
  const setCenterView = useRunViewStore((s) => s.setCenterView);
  const compact = useRunViewStore((s) => s.headerCompact);
  const setHeaderCompact = useRunViewStore((s) => s.setHeaderCompact);
  const { setRunStreamSlot } = useStudioShellContext();

  // The live page URL comes from the stream frames (CDP); reset per run.
  const [streamUrl, setStreamUrl] = useState<string | null>(null);
  // Block runs default to the live debug stream; this opts into the recording.
  const [recordingOpen, setRecordingOpen] = useState(false);
  // The failure banner is dismissable; the route reuses this instance across
  // runs, so clear the dismissal whenever the run changes.
  const [failureDismissed, setFailureDismissed] = useState(false);
  useEffect(() => {
    setStreamUrl(null);
    setRecordingOpen(false);
    setFailureDismissed(false);
  }, [workflowRunId]);

  // Collapse labels to icons by the header's own width — the studio side panels
  // make viewport width the wrong signal.
  const headerRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = headerRef.current;
    if (!el || typeof ResizeObserver === "undefined") {
      return;
    }
    const observer = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width ?? 0;
      setHeaderCompact(width < HEADER_COMPACT_BELOW_PX);
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [setHeaderCompact]);

  const scrubbing = pinnedFrameId != null && pinnedFrameId !== "stream";
  const hasRecording = recordingUrls.length > 0;

  // An explicit tab wins; otherwise a block run defaults to the live debug
  // stream and a full run to its stream (running) / recording / screenshot.
  const center: CenterView =
    centerView === "code"
      ? "code"
      : centerView === "inputs" && inputs
        ? "inputs"
        : centerView === "outputs" && outputs
          ? "outputs"
          : scrubbing
            ? "screenshot"
            : showDebugStream
              ? recordingOpen && hasRecording
                ? "recording"
                : "stream"
              : running
                ? "stream"
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
          : center === "inputs"
            ? "Inputs"
            : center === "outputs"
              ? "Outputs"
              : (shownFrame?.label ?? "Screenshot");

  const headerIcon =
    center === "recording" ? (
      <PlayIcon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
    ) : center === "code" ? (
      <CodeIcon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
    ) : center === "inputs" ? (
      <ListBulletIcon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
    ) : center === "outputs" ? (
      <FileTextIcon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
    ) : (
      <GlobeIcon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
    );

  const toggleCenter = (view: "code" | "inputs" | "outputs") =>
    setCenterView(centerView === view ? "default" : view);

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-lg border border-border bg-slate-elevation1">
      <div
        ref={headerRef}
        className="flex items-center gap-2 border-b border-border px-3 py-2"
      >
        <div className="flex min-w-0 flex-1 items-center gap-1.5">
          {headerIcon}
          <span className="min-w-0 flex-1 truncate text-sm font-medium text-foreground">
            {headerLabel}
          </span>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {showDebugStream ? (
            <>
              <ViewToggle
                active={center === "stream"}
                onClick={() => {
                  setRecordingOpen(false);
                  pinFrame("stream");
                }}
                compact={compact}
                label="Live"
                icon={
                  <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-success" />
                }
              />
              {hasRecording ? (
                <ViewToggle
                  active={center === "recording"}
                  onClick={() => {
                    setRecordingOpen(true);
                    jumpToLive();
                  }}
                  compact={compact}
                  label="Recording"
                  icon={<PlayIcon className="h-3 w-3" />}
                />
              ) : null}
            </>
          ) : running ? (
            <ViewToggle
              active={center === "stream"}
              onClick={() => pinFrame("stream")}
              compact={compact}
              label="Live"
              icon={
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-success" />
              }
            />
          ) : hasRecording ? (
            <ViewToggle
              active={center === "recording"}
              onClick={jumpToLive}
              compact={compact}
              label="Recording"
              icon={<PlayIcon className="h-3 w-3" />}
            />
          ) : null}
          <ViewToggle
            active={center === "code"}
            onClick={() => toggleCenter("code")}
            compact={compact}
            label="Code"
            icon={<CodeIcon className="h-3 w-3" />}
          />
          {inputs ? (
            <ViewToggle
              active={center === "inputs"}
              onClick={() => toggleCenter("inputs")}
              compact={compact}
              label="Inputs"
              icon={<ListBulletIcon className="h-3 w-3" />}
            />
          ) : null}
          {outputs ? (
            <ViewToggle
              active={center === "outputs"}
              onClick={() => toggleCenter("outputs")}
              compact={compact}
              label="Outputs"
              icon={<FileTextIcon className="h-3 w-3" />}
            />
          ) : null}
          {overview || actions ? (
            <div className="mx-0.5 h-4 w-px bg-border" />
          ) : null}
          {overview}
          {actions}
          {compact ? null : (
            <span
              className="ml-1 whitespace-nowrap font-mono text-[11px] tabular-nums text-muted-foreground"
              title="Elapsed"
            >
              {elapsed}
            </span>
          )}
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
        ) : center === "inputs" ? (
          <div className="absolute inset-0 overflow-y-auto bg-slate-elevation1 p-4">
            {inputs}
          </div>
        ) : center === "outputs" ? (
          <div className="absolute inset-0 overflow-y-auto bg-slate-elevation1 p-4">
            {outputs}
          </div>
        ) : center === "stream" ? (
          showDebugStream ? (
            // The shell re-parents the persistent debug-session stream into this
            // slot (the same node as the Browser tab), so a block run shows its
            // live browser here, view-only. The slot unmounts when the user scrubs
            // or opens code/recording, which parks the node back offscreen.
            <div
              ref={setRunStreamSlot}
              data-testid="run-stream-slot"
              className="absolute inset-0"
            />
          ) : provisioning ? (
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
          <HeroScreenshot
            workflowRunBlockId={shownFrame.blockId}
            blockType={shownFrame.blockType}
            running={running}
          />
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
            {running || showDebugStream ? (
              <button
                type="button"
                onClick={() => {
                  setRecordingOpen(false);
                  pinFrame("stream");
                }}
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

        {failed &&
        !failureDismissed &&
        !scrubbing &&
        (center === "screenshot" ||
          (showDebugStream && center === "stream")) ? (
          <div className="absolute inset-x-0 bottom-0 m-4 rounded-lg border border-destructive/40 bg-slate-elevation1/95 p-4 shadow-lg backdrop-blur">
            <div className="flex items-start gap-2 text-sm font-semibold text-foreground">
              <ExclamationTriangleIcon className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
              <span className="min-w-0 flex-1">
                {failureReason ?? "The run failed."}
              </span>
              <button
                type="button"
                onClick={() => setFailureDismissed(true)}
                className="-mr-1 -mt-1 shrink-0 rounded p-1 text-muted-foreground hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                aria-label="Dismiss"
                title="Dismiss"
              >
                <Cross2Icon className="h-4 w-4" />
              </button>
            </div>
            <div className="mt-3 flex flex-wrap gap-2">
              {onFix ? (
                <Button
                  size="sm"
                  className="bg-studio-accent text-foreground hover:bg-studio-accent/90"
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
