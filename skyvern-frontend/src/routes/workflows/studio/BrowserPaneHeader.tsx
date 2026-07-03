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
import { useRecordingLauncherStore } from "@/store/useRecordingLauncherStore";
import { useRecordingStore } from "@/store/useRecordingStore";
import { useStudioBrowserStore } from "@/store/useStudioBrowserStore";
import { cn } from "@/util/utils";

import { useBrowserPaneView } from "./useBrowserPaneView";
import { useStudioPaneCompact } from "./StudioShellContext";
import { ViewToggle } from "./ViewToggle";

const ICON_BUTTON =
  "shrink-0 rounded p-1 text-muted-foreground hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-40";

const RECORDING_ARCHIVED_LABEL =
  "Recording archived — contact support@skyvern.com to request restoration";

export function BrowserPaneViewPills() {
  const compact = useStudioPaneCompact();
  const { browserStreamingMode } = useBrowserStreamingMode();
  const { view, setView, visuals } = useBrowserPaneView();
  const hasRecording = visuals.recordingUrls.length > 0;

  return (
    <>
      {compact ? null : (
        <StreamModeBadge mode={browserStreamingMode} className="shrink-0" />
      )}
      <div
        role="group"
        aria-label="Browser view"
        className="flex shrink-0 items-center gap-0.5 rounded-md border border-slate-700 bg-slate-elevation2 p-0.5"
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
        {hasRecording ? (
          <ViewToggle
            active={view === "recording"}
            onClick={() => setView("recording")}
            compact={compact}
            label="Recording"
            icon={<PlayIcon className="h-3 w-3" />}
          />
        ) : visuals.recordingArchived ? (
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
        {visuals.hasScreenshots ? (
          <ViewToggle
            active={view === "screenshots"}
            onClick={() => setView("screenshots")}
            compact={compact}
            label="Screenshots"
            icon={<ImageIcon className="h-3 w-3" />}
          />
        ) : null}
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
  const startRecordingAtEnd = useRecordingLauncherStore(
    (s) => s.startRecordingAtEnd,
  );
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
      {isRecording ? (
        <span
          className={cn(
            "flex shrink-0 items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium",
            manualCapturePaused
              ? "bg-amber-500/10 text-amber-500"
              : "bg-red-500/10 text-red-500",
          )}
        >
          <span
            className={cn(
              "h-2 w-2 rounded-full",
              manualCapturePaused ? "bg-amber-500" : "animate-pulse bg-red-500",
            )}
          />
          {compact ? null : manualCapturePaused ? "Paused" : "Recording"}
        </span>
      ) : (
        <Button
          variant="secondary"
          size="sm"
          className="h-7 shrink-0 gap-1.5 px-2 text-xs"
          title="Record browser actions into blocks"
          disabled={!browserSessionId || !startRecordingAtEnd}
          onClick={() => startRecordingAtEnd?.()}
        >
          <span className="h-2 w-2 rounded-full bg-red-500" />
          {compact ? null : "Record"}
        </Button>
      )}
      <button
        type="button"
        title={debugHidden ? blockedTitle : "Reconnect"}
        aria-label="Reconnect browser stream"
        onClick={reload}
        disabled={!browserSessionId || debugHidden}
        className={ICON_BUTTON}
      >
        <ReloadIcon className="h-3.5 w-3.5" />
      </button>
      <button
        type="button"
        title={debugHidden ? blockedTitle : "Open in new tab"}
        aria-label="Open browser in new tab"
        onClick={openInNewTab}
        disabled={!browserSessionId || debugHidden}
        className={ICON_BUTTON}
      >
        <OpenInNewWindowIcon className="h-3.5 w-3.5" />
      </button>
      <Dialog
        open={confirmOff}
        onOpenChange={(open) => {
          if (!open && cycleBrowser.isPending) {
            return;
          }
          setConfirmOff(open);
        }}
      >
        <DialogTrigger asChild>
          <button
            type="button"
            title={debugHidden ? blockedTitle : "Turn off browser"}
            aria-label="Turn off browser"
            disabled={!workflowPermanentId || !browserSessionId || debugHidden}
            className={ICON_BUTTON}
          >
            <PowerIcon className="h-3.5 w-3.5" />
          </button>
        </DialogTrigger>
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
