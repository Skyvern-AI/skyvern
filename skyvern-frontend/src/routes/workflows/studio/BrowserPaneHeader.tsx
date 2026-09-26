import {
  DotsHorizontalIcon,
  GlobeIcon,
  ImageIcon,
  OpenInNewWindowIcon,
  PlayIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { AxiosError } from "axios";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { useWorkflowPermanentId } from "@/routes/workflows/WorkflowPermanentIdContext";
import { runIsRetryWaiting } from "@/routes/workflows/workflowRun/runRetryState";
import type { StreamState } from "@/routes/streaming/streamState";

import { getClient } from "@/api/AxiosClient";
import { DebugSessionApiResponse } from "@/api/types";
import { PowerIcon } from "@/components/icons/PowerIcon";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useRecordingLauncherStore } from "@/store/useRecordingLauncherStore";
import { useRecordingStore } from "@/store/useRecordingStore";
import { useSettingsStore } from "@/store/SettingsStore";
import { useStudioBrowserStore } from "@/store/useStudioBrowserStore";
import { compactLocalDateTime } from "@/util/timeFormat";
import { cn } from "@/util/utils";

import { PANE_HEADER_ICON_BUTTON_CLASS } from "./constants";
import { ControlTooltip } from "./ControlTooltip";
import { useBrowserPaneView } from "./useBrowserPaneView";
import { useStudioPaneCompact } from "./StudioShellContext";
import { ViewToggle } from "./ViewToggle";

const MENU_ITEM_CLASS =
  "cursor-pointer gap-2 rounded px-2 py-1.5 pr-3 text-xs font-medium focus:text-foreground";

const FINISHED_RUN_BROWSER_LABEL =
  "This run has finished — shows the agent's debug browser, not the run";

// Status, not a switch: with no run open there is nothing to toggle to, but
// the user still needs to know whether the browser they see is up yet.
function BrowserLiveStatus({
  state,
  stoppedHint,
}: {
  state: "starting" | "live" | "stopped";
  stoppedHint?: string;
}) {
  return (
    <span
      role="status"
      data-testid="browser-pane-live-status"
      title={state === "stopped" ? stoppedHint : undefined}
      className="inline-flex h-7 shrink-0 items-center gap-1.5 px-1.5 text-xs font-medium text-muted-foreground"
    >
      {state === "starting" ? (
        <ReloadIcon aria-hidden className="h-3 w-3 motion-safe:animate-spin" />
      ) : (
        <span
          aria-hidden
          className={cn(
            "h-1.5 w-1.5 rounded-full",
            state === "live"
              ? "bg-success motion-safe:animate-pulse"
              : "bg-muted-foreground/50",
          )}
        />
      )}
      {state === "starting"
        ? "Starting browser…"
        : state === "live"
          ? "Live"
          : "Browser stopped"}
    </span>
  );
}

function statusFromStream(
  stream: StreamState | undefined,
): "starting" | "live" | "stopped" {
  return stream === "live" || stream === "stopped" ? stream : "starting";
}

export function BrowserPaneViewPills() {
  const compact = useStudioPaneCompact();
  const {
    view,
    setView,
    visuals,
    inspectingRun,
    liveSurface,
    debugBrowserSessionId,
    runId,
  } = useBrowserPaneView();
  const loadingBrowser = useSettingsStore((s) => s.isLoadingABrowser);
  // Studio's stream never reports ready to the route, so isLoadingABrowser
  // alone stays true after the browser is up.
  const debugStream = useStudioBrowserStore((s) => s.debugStream);
  const runStream = useStudioBrowserStore((s) => s.runStream);
  const runCreatedAt = visuals.workflowRun
    ? compactLocalDateTime(visuals.workflowRun.created_at)
    : "";
  const runLabel = runCreatedAt ? (
    // Grows from zero into leftover space, truncating before the title or
    // buttons shrink; under 700px its frame alone would squeeze them.
    <span
      data-testid="browser-pane-run-label"
      className="hidden min-w-0 max-w-fit grow-[999] basis-0 truncate rounded-full border px-2 py-0.5 text-[11px] text-muted-foreground [@container_pane-header_(min-width:700px)]:block"
    >
      Run · {runCreatedAt}
    </span>
  ) : null;

  // Recording and Screenshots only ever replay a workflow run; with none open
  // they would point at an old run or at nothing, and Live alone is no switch.
  if (!inspectingRun) {
    // The latest run is running in its own browser, so Live shows that run
    // rather than the debug browser: name it.
    if (liveSurface === "run") {
      // Same conditions under which the pane body holds the stream back.
      const runStarting =
        visuals.provisioning ||
        (visuals.workflowRun != null && runIsRetryWaiting(visuals.workflowRun));
      // Run status alone says Live while the stream is still connecting or
      // after it dropped; only the stream knows whether frames are painting.
      const runState = runStarting
        ? "starting"
        : statusFromStream(
            runStream && runStream.workflowRunId === runId
              ? runStream.state
              : undefined,
          );
      return (
        <>
          {runLabel}
          <BrowserLiveStatus
            state={runState}
            stoppedHint="The run's browser stream ended"
          />
        </>
      );
    }
    if (
      debugBrowserSessionId &&
      debugStream &&
      debugStream.browserSessionId === debugBrowserSessionId &&
      debugStream.state !== "connecting"
    ) {
      return (
        <BrowserLiveStatus
          state={debugStream.state}
          stoppedHint="Use ⋯ → Reconnect stream or Restart browser"
        />
      );
    }
    return debugBrowserSessionId || loadingBrowser ? (
      <BrowserLiveStatus state="starting" />
    ) : null;
  }

  // Sitting beside the inspected run's replay pills, a pulsing "Live" reads as
  // the run's own status. Once that run is over this view is the debug browser
  // — a surface the run left behind — so it has to say so.
  const finishedRun = visuals.finalized;

  return (
    <>
      {runLabel}
      <div
        role="group"
        aria-label="Browser view"
        className="flex shrink-0 items-center gap-1"
      >
        <ViewToggle
          active={view === "live"}
          onClick={() => setView("live")}
          compact={compact}
          label={finishedRun ? "Debug browser" : "Live"}
          title={finishedRun ? FINISHED_RUN_BROWSER_LABEL : undefined}
          icon={
            finishedRun ? (
              <GlobeIcon className="h-3 w-3" />
            ) : (
              <span className="h-1.5 w-1.5 rounded-full bg-success motion-safe:animate-pulse" />
            )
          }
        />
        <ViewToggle
          active={view === "recording"}
          onClick={() => setView("recording")}
          compact={compact}
          label="Recording"
          icon={<PlayIcon className="h-3 w-3" />}
        />
        <ViewToggle
          active={view === "screenshots"}
          onClick={() => setView("screenshots")}
          compact={compact}
          label="Screenshots"
          icon={<ImageIcon className="h-3 w-3" />}
        />
      </div>
    </>
  );
}

export function BrowserPaneActions() {
  const workflowPermanentId = useWorkflowPermanentId();
  const compact = useStudioPaneCompact();
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const { debugBrowserSessionId: browserSessionId, liveSurface } =
    useBrowserPaneView();
  const isRecording = useRecordingStore((s) => s.isRecording);
  const startRecordingAtEnd = useRecordingLauncherStore(
    (s) => s.startRecordingAtEnd,
  );
  // These act on the debug browser; while the pane streams the run's own
  // browser instead, they'd hit an invisible session. Record explains itself
  // with a disabled tooltip; the ⋯ menu's session actions are hidden.
  const debugHidden = liveSurface === "run";
  const blockedTitle =
    "Showing the run's browser — debug browser controls come back after the run";
  const reload = useStudioBrowserStore((s) => s.reload);
  const [confirmRestart, setConfirmRestart] = useState(false);
  const moreActionsRef = useRef<HTMLButtonElement>(null);

  const cycleBrowser = useMutation({
    mutationFn: async (workflowId: string) => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client.post<DebugSessionApiResponse>(
        `/debug-session/${workflowId}/new`,
      );
    },
    onSuccess: (response) => {
      queryClient.setQueryData(
        ["debugSession", workflowPermanentId],
        response.data,
      );
      void queryClient.invalidateQueries({
        queryKey: ["debugSession", workflowPermanentId],
      });
      setConfirmRestart(false);
      toast({
        variant: "success",
        title: "Browser restarted",
        description: "A fresh browser is starting.",
      });
    },
    onError: (error: AxiosError) => {
      toast({
        variant: "destructive",
        title: "Failed to restart browser",
        description: error.message,
      });
    },
  });

  const openInNewTab = () => {
    if (!browserSessionId) {
      return;
    }
    window.open(
      `${window.location.origin}/browser-session/${browserSessionId}`,
      "_blank",
      "noopener",
    );
  };

  return (
    <>
      {!isRecording
        ? (() => {
            const disabled =
              !browserSessionId || debugHidden || !startRecordingAtEnd;
            const tooltip = debugHidden
              ? blockedTitle
              : startRecordingAtEnd
                ? "Complete the task in the browser. Skyvern captures the browser view and your clicks, typing, and navigation, then turns them into workflow steps."
                : "Recording is available when the editor is ready";
            return (
              <ControlTooltip
                content={<span className="block max-w-[260px]">{tooltip}</span>}
                blocked={disabled}
              >
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-7 shrink-0 gap-1.5 px-1.5 text-red-500"
                  aria-label="Record task"
                  disabled={disabled}
                  onClick={() => startRecordingAtEnd?.()}
                >
                  <span className="h-2 w-2 rounded-full bg-red-500" />
                  {compact ? null : "Record task"}
                </Button>
              </ControlTooltip>
            );
          })()
        : null}
      {browserSessionId && !debugHidden ? (
        <DropdownMenu>
          <ControlTooltip content="More browser actions">
            <DropdownMenuTrigger asChild>
              <button
                ref={moreActionsRef}
                type="button"
                aria-label="More browser actions"
                className={PANE_HEADER_ICON_BUTTON_CLASS}
              >
                <DotsHorizontalIcon className="h-3.5 w-3.5" />
              </button>
            </DropdownMenuTrigger>
          </ControlTooltip>
          <DropdownMenuContent align="end" sideOffset={6} className="min-w-44">
            <DropdownMenuItem
              onSelect={reload}
              className={cn(MENU_ITEM_CLASS, "text-muted-foreground")}
            >
              <ReloadIcon className="h-3.5 w-3.5" />
              Reconnect stream
            </DropdownMenuItem>
            <DropdownMenuItem
              onSelect={openInNewTab}
              className={cn(MENU_ITEM_CLASS, "text-muted-foreground")}
            >
              <OpenInNewWindowIcon className="h-3.5 w-3.5" />
              Open in new tab
            </DropdownMenuItem>
            {workflowPermanentId ? (
              <>
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  // Defer so the dialog's focus trap doesn't mount while the
                  // closing menu's trap is still live; the two fight over focus.
                  onSelect={() => setTimeout(() => setConfirmRestart(true), 0)}
                  className={cn(
                    MENU_ITEM_CLASS,
                    "text-destructive focus:bg-destructive/10 focus:text-destructive",
                  )}
                >
                  <PowerIcon className="h-3.5 w-3.5" />
                  Restart browser…
                </DropdownMenuItem>
              </>
            ) : null}
          </DropdownMenuContent>
        </DropdownMenu>
      ) : null}
      <Dialog
        open={confirmRestart}
        onOpenChange={(open) => {
          if (!open && cycleBrowser.isPending) {
            return;
          }
          setConfirmRestart(open);
        }}
      >
        <DialogContent
          // Opened from the menu, so the dialog has no trigger of its own to
          // hand focus back to; return it to the ⋯ that led here.
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            moreActionsRef.current?.focus();
          }}
        >
          <DialogHeader>
            <DialogTitle>Restart this browser?</DialogTitle>
            <DialogDescription>
              This ends the current browser and starts a fresh one. Anything in
              progress here will stop.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <DialogClose asChild>
              <Button variant="secondary" disabled={cycleBrowser.isPending}>
                Cancel
              </Button>
            </DialogClose>
            <Button
              variant="destructive"
              disabled={!workflowPermanentId || cycleBrowser.isPending}
              onClick={() =>
                workflowPermanentId && cycleBrowser.mutate(workflowPermanentId)
              }
            >
              {cycleBrowser.isPending ? (
                <>
                  <ReloadIcon className="mr-2 size-4 animate-spin" />
                  Restarting…
                </>
              ) : (
                "Restart"
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
