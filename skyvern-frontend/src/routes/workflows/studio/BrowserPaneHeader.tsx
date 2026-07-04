import {
  ImageIcon,
  OpenInNewWindowIcon,
  PlayIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { AxiosError } from "axios";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useParams } from "react-router-dom";

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
  DialogTrigger,
} from "@/components/ui/dialog";
import { toast } from "@/components/ui/use-toast";
import { useBrowserStreamingMode } from "@/hooks/useRuntimeConfig";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { StreamModeBadge } from "@/routes/streaming/StreamDiagnostics";
import { useRecordingStore } from "@/store/useRecordingStore";
import { useStudioBrowserStore } from "@/store/useStudioBrowserStore";
import { cn } from "@/util/utils";

import { ControlTooltip } from "./ControlTooltip";
import { PaneHeaderDivider } from "./PaneHeaderDivider";
import { useBrowserPaneView } from "./useBrowserPaneView";
import { useStudioPaneCompact } from "./StudioShellContext";
import { ViewToggle } from "./ViewToggle";

const ICON_BUTTON =
  "inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-border text-muted-foreground hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-40";

const RECORDING_ARCHIVED_LABEL =
  "Recording archived — contact support@skyvern.com to request restoration";

export function BrowserPaneViewPills() {
  const compact = useStudioPaneCompact();
  const { browserStreamingMode } = useBrowserStreamingMode();
  const { view, setView, visuals } = useBrowserPaneView();
  const hasRecording = visuals.recordingUrls.length > 0;

  return (
    <>
      {/* Stream-transport diagnostics are a local-dev aid; deployed builds
          keep the header clean. */}
      {compact || !import.meta.env.DEV ? null : (
        <StreamModeBadge mode={browserStreamingMode} className="shrink-0" />
      )}
      <PaneHeaderDivider />
      <div
        role="group"
        aria-label="Browser view"
        className="flex shrink-0 items-center gap-1"
      >
        <ViewToggle
          active={view === "live"}
          onClick={() => setView("live")}
          compact={compact}
          label="Live"
          icon={
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-success" />
          }
        />
        {!hasRecording && visuals.recordingArchived ? (
          <ControlTooltip content={RECORDING_ARCHIVED_LABEL} blocked>
            <button
              type="button"
              disabled
              aria-label={RECORDING_ARCHIVED_LABEL}
              className="pointer-events-none inline-flex items-center gap-1.5 rounded px-2 py-1 text-[11px] font-medium text-muted-foreground opacity-60"
            >
              <PlayIcon className="h-3 w-3" />
              {compact ? null : "Recording archived"}
            </button>
          </ControlTooltip>
        ) : (
          <ViewToggle
            active={view === "recording"}
            onClick={() => setView("recording")}
            compact={compact}
            label="Recording"
            icon={<PlayIcon className="h-3 w-3" />}
          />
        )}
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
  const { workflowPermanentId } = useParams();
  const compact = useStudioPaneCompact();
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const { debugBrowserSessionId: browserSessionId, liveSurface } =
    useBrowserPaneView();
  const isRecording = useRecordingStore((s) => s.isRecording);
  const manualCapturePaused = useRecordingStore((s) => s.manualCapturePaused);
  const finishRequested = useRecordingStore((s) => s.finishRequested);
  // These act on the debug browser; while the pane streams the run's own
  // browser instead, they'd hit an invisible session — disable with a reason.
  const debugHidden = liveSurface === "run";
  const blockedTitle =
    "Showing the run's browser — debug browser controls come back after the run";
  const reload = useStudioBrowserStore((s) => s.reload);
  const [confirmOff, setConfirmOff] = useState(false);

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
      setConfirmOff(false);
      toast({
        variant: "success",
        title: "Browser restarted",
        description: "A fresh browser is starting.",
      });
    },
    onError: (error: AxiosError) => {
      toast({
        variant: "destructive",
        title: "Failed to turn off browser",
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
      {isRecording
        ? (() => {
            // Same finish path as the drafts panel's Done: requestFinish stops
            // capture and the mounted RecordingPanel commits the recorded steps.
            const stopButton = (
              <Button
                variant="outline"
                size="sm"
                className={cn(
                  "h-7 shrink-0 gap-1.5 border-border bg-transparent px-2 text-xs shadow-none",
                  manualCapturePaused ? "text-amber-500" : "text-red-500",
                )}
                aria-label="Stop recording"
                disabled={finishRequested}
                onClick={() => useRecordingStore.getState().requestFinish()}
              >
                <span
                  className={cn(
                    "h-2 w-2 rounded-full",
                    manualCapturePaused
                      ? "bg-amber-500"
                      : "animate-pulse bg-red-500",
                  )}
                />
                {compact ? null : finishRequested ? "Stopping…" : "Stop"}
              </Button>
            );
            // Labelled → no tooltip; compact collapses to the dot, so the
            // tooltip carries the action.
            return compact ? (
              <ControlTooltip
                content="Stop recording and save the recorded steps"
                blocked={finishRequested}
              >
                {stopButton}
              </ControlTooltip>
            ) : (
              stopButton
            );
          })()
        : null}
      <ControlTooltip
        content={debugHidden ? blockedTitle : "Reconnect"}
        blocked={!browserSessionId || debugHidden}
      >
        <button
          type="button"
          aria-label="Reconnect browser stream"
          onClick={reload}
          disabled={!browserSessionId || debugHidden}
          className={ICON_BUTTON}
        >
          <ReloadIcon className="h-3.5 w-3.5" />
        </button>
      </ControlTooltip>
      <ControlTooltip
        content={debugHidden ? blockedTitle : "Open in new tab"}
        blocked={!browserSessionId || debugHidden}
      >
        <button
          type="button"
          aria-label="Open browser in new tab"
          onClick={openInNewTab}
          disabled={!browserSessionId || debugHidden}
          className={ICON_BUTTON}
        >
          <OpenInNewWindowIcon className="h-3.5 w-3.5" />
        </button>
      </ControlTooltip>
      <Dialog
        open={confirmOff}
        onOpenChange={(open) => {
          if (!open && cycleBrowser.isPending) {
            return;
          }
          setConfirmOff(open);
        }}
      >
        <ControlTooltip
          content={debugHidden ? blockedTitle : "Turn off browser"}
          blocked={!workflowPermanentId || !browserSessionId || debugHidden}
        >
          <DialogTrigger asChild>
            <button
              type="button"
              aria-label="Turn off browser"
              disabled={
                !workflowPermanentId || !browserSessionId || debugHidden
              }
              className={ICON_BUTTON}
            >
              <PowerIcon className="h-3.5 w-3.5" />
            </button>
          </DialogTrigger>
        </ControlTooltip>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Turn off this browser?</DialogTitle>
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
                  Turning off…
                </>
              ) : (
                "Turn off"
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
