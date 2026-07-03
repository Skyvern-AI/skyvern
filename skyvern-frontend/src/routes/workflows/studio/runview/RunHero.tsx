import { type ReactNode, useEffect, useRef, useState } from "react";
import {
  ClockIcon,
  CodeIcon,
  CounterClockwiseClockIcon,
  Cross2Icon,
  ExclamationTriangleIcon,
  FileTextIcon,
  GlobeIcon,
  ImageIcon,
  ListBulletIcon,
  PlayIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";

import { Button } from "@/components/ui/button";
import { StreamStatusPanel } from "@/routes/streaming/StreamDiagnostics";
import { useRunViewStore } from "@/store/RunViewStore";
import { cn } from "@/util/utils";

import { WorkflowRunCode } from "../../workflowRun/WorkflowRunCode";
import { useStudioShellContext } from "../StudioShellContext";
import { matchFailureTips } from "./failureTips";
import { HeroRecording } from "./HeroRecording";
import { HeroScreenshot, type HeroSelection } from "./HeroScreenshot";
import { resolveRunHeroCenterView } from "./runHeroCenter";
import { RunLiveStream } from "./RunLiveStream";

type RunHeroProps = {
  workflowRunId: string;
  heroSelection: HeroSelection | null;
  heroLabel: string;
  running: boolean;
  // A block run shows the shared debug-session stream (re-parented in by the
  // shell), view-only, instead of mounting a separate run stream.
  showDebugStream: boolean;
  // The open Browser pane outranks this hero for the single stream node; the
  // Live affordance points there instead of registering a slot that stays black.
  debugStreamInBrowserPane?: boolean;
  onFocusBrowserPane?: () => void;
  // The pane keeps rendering while closed (display:none); never mount a stream
  // socket into a hidden pane.
  paneOpen?: boolean;
  provisioning: boolean;
  isPaused: boolean;
  failed: boolean;
  failureReason: string | null;
  codeGenerating?: boolean;
  browserSessionId: string | null;
  recordingUrls: string[];
  recordingArchived?: boolean;
  hasScreenshots?: boolean;
  elapsed: string;
  inputs?: ReactNode;
  outputs?: ReactNode;
  overview?: ReactNode;
  actions?: ReactNode;
  onRecordingPlay?: (index: number) => void;
  onFix?: () => void;
  onRetry?: () => void;
};

// Below this header width the toggle/dropdown labels collapse to icons.
const HEADER_COMPACT_BELOW_PX = 640;

const RECORDING_ARCHIVED_LABEL =
  "Recording archived — contact support@skyvern.com to request restoration";

function ViewToggle({
  active,
  onClick,
  icon,
  label,
  compact,
  title,
}: {
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  label: string;
  compact: boolean;
  title?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title ?? (compact ? label : undefined)}
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
  heroSelection,
  heroLabel,
  running,
  showDebugStream,
  debugStreamInBrowserPane = false,
  onFocusBrowserPane,
  paneOpen = true,
  provisioning,
  isPaused,
  failed,
  failureReason,
  codeGenerating = false,
  browserSessionId,
  recordingUrls,
  recordingArchived = false,
  hasScreenshots = false,
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

  const center = resolveRunHeroCenterView({
    centerView,
    hasScreenshots,
    hasInputs: Boolean(inputs),
    hasOutputs: Boolean(outputs),
    hasRecording,
    scrubbing,
    showDebugStream,
    debugStreamInBrowserPane,
    recordingOpen,
    running,
    failed,
  });

  const focusBrowserPane = () => {
    setRecordingOpen(false);
    // Unpin instead of pinning "stream": the hero can't host the debug node,
    // so its screenshot surface follows the live edge while the pane streams.
    jumpToLive();
    onFocusBrowserPane?.();
  };

  // The right-side toggles own the view identity (Live/Recording/Code) and the
  // in-card "Inspecting step" bar owns a scrubbed action's description; the
  // header only adds context they don't surface — the live page URL, or the
  // final frame's label when nothing else names it.
  const headerLabel =
    center === "stream"
      ? streamUrl
      : center === "screenshot" && !scrubbing
        ? heroLabel
        : null;

  const toggleCenter = (view: "code" | "inputs" | "outputs") =>
    setCenterView(centerView === view ? "default" : view);
  const showRunToggle =
    Boolean(heroSelection) || centerView === "screenshot" || failed;

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-lg border border-border bg-slate-elevation1">
      <div
        ref={headerRef}
        className="flex items-center gap-2 border-b border-border px-3 py-2"
      >
        <div
          role="group"
          aria-label="Center view"
          className="flex shrink-0 items-center gap-0.5 rounded-md border border-slate-700 bg-slate-elevation2 p-0.5"
        >
          {showDebugStream ? (
            <>
              <ViewToggle
                active={!debugStreamInBrowserPane && center === "stream"}
                onClick={
                  debugStreamInBrowserPane
                    ? focusBrowserPane
                    : () => {
                        setRecordingOpen(false);
                        pinFrame("stream");
                      }
                }
                compact={compact}
                label="Live"
                title={
                  debugStreamInBrowserPane
                    ? "Watch live in the Browser pane"
                    : undefined
                }
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
            <>
              {showRunToggle ? (
                <ViewToggle
                  active={center === "screenshot"}
                  onClick={() => setCenterView("screenshot")}
                  compact={compact}
                  label="Run"
                  icon={<CounterClockwiseClockIcon className="h-3 w-3" />}
                />
              ) : null}
              <ViewToggle
                active={center === "recording"}
                onClick={() => setCenterView("recording")}
                compact={compact}
                label="Recording"
                icon={<PlayIcon className="h-3 w-3" />}
              />
            </>
          ) : recordingArchived ? (
            <button
              type="button"
              disabled
              title={RECORDING_ARCHIVED_LABEL}
              aria-label={RECORDING_ARCHIVED_LABEL}
              className="inline-flex cursor-not-allowed items-center gap-1.5 rounded px-2 py-1 text-[11px] font-medium text-muted-foreground opacity-60"
            >
              <PlayIcon className="h-3 w-3" />
              {compact ? null : "Recording archived"}
            </button>
          ) : null}
          {hasScreenshots ? (
            <ViewToggle
              active={center === "screenshot"}
              onClick={() => setCenterView("screenshots")}
              compact={compact}
              label="Screenshots"
              icon={<ImageIcon className="h-3 w-3" />}
            />
          ) : null}
          <ViewToggle
            active={center === "code"}
            onClick={() => toggleCenter("code")}
            compact={compact}
            label="Code"
            title={
              codeGenerating ? "Generating cached code for this run" : undefined
            }
            icon={
              codeGenerating ? (
                <ReloadIcon
                  data-testid="code-generating-spinner"
                  className="h-3 w-3 animate-spin"
                />
              ) : (
                <CodeIcon className="h-3 w-3" />
              )
            }
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
        </div>
        <div className="flex min-w-0 flex-1 items-center gap-1.5">
          {headerLabel ? (
            <>
              <GlobeIcon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
              <span className="min-w-0 flex-1 truncate text-sm font-medium text-foreground">
                {headerLabel}
              </span>
            </>
          ) : null}
        </div>
        <div className="flex shrink-0 items-center gap-1">
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
          !paneOpen ? null : showDebugStream ? (
            // The shell re-parents the persistent debug-session stream into this
            // slot (the same node as the Browser pane), so a block run shows its
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
        ) : heroSelection ? (
          <HeroScreenshot selection={heroSelection} running={running} />
        ) : (
          <div className="absolute inset-0 grid place-items-center text-sm text-muted-foreground">
            Waiting for the first action…
          </div>
        )}

        {provisioning &&
        showDebugStream &&
        !scrubbing &&
        (center === "stream" || center === "screenshot") ? (
          // A block run can queue behind a running full run (run_sequentially);
          // the debug browser is already live, so say why nothing moves yet.
          <div className="absolute left-3 top-3 flex items-center gap-2 rounded-md bg-black/70 px-3 py-1.5 text-xs text-white backdrop-blur">
            <ClockIcon className="h-3.5 w-3.5 shrink-0" />
            <span>Run queued — waiting to start</span>
          </div>
        ) : null}

        {center === "screenshot" && scrubbing && heroSelection ? (
          <div className="absolute left-3 top-3 flex max-w-[26rem] items-center gap-2 rounded-md bg-black/70 px-3 py-1.5 text-xs text-white backdrop-blur">
            <CounterClockwiseClockIcon className="h-3.5 w-3.5 shrink-0" />
            <span className="truncate">
              Inspecting · <b>{heroLabel}</b>
            </span>
            {running || showDebugStream ? (
              <button
                type="button"
                onClick={
                  showDebugStream && debugStreamInBrowserPane
                    ? focusBrowserPane
                    : () => {
                        setRecordingOpen(false);
                        pinFrame("stream");
                      }
                }
                className="ml-1 inline-flex items-center gap-1.5 rounded-full bg-white/15 px-2 py-0.5 text-[11px] hover:bg-white/25 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-white"
              >
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-success" />
                Jump to live
              </button>
            ) : hasRecording ? (
              <button
                type="button"
                onClick={() => {
                  // Preserve the inspected frame so "Run" returns to this step.
                  setCenterView("recording");
                }}
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
                {matchFailureTips(failureReason).map((tip) => (
                  <span
                    key={tip}
                    className="mt-1.5 block text-xs font-normal italic text-muted-foreground"
                  >
                    {tip}
                  </span>
                ))}
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
