import { Cross2Icon, ReloadIcon } from "@radix-ui/react-icons";
import { useEffect, useState } from "react";
import {
  NavLink,
  Outlet,
  useLocation,
  useParams,
  useSearchParams,
} from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { cn } from "@/util/utils";

import { getClient } from "@/api/AxiosClient";
import { Badge } from "@/components/ui/badge";
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
import { BrowserStream } from "@/components/BrowserStream";
import { BrowserIcon } from "@/components/icons/BrowserIcon";
import { Toaster } from "@/components/ui/toaster";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useCloseBrowserSessionMutation } from "@/routes/browserSessions/hooks/useCloseBrowserSessionMutation";
import { SaveSessionAsBrowserProfileDialog } from "@/routes/browserProfiles/SaveSessionAsBrowserProfileDialog";
import { useBackgroundBrowserProfileCreate } from "@/routes/browserProfiles/hooks/useBackgroundBrowserProfileCreate";
import { useBrowserProfileCreateStore } from "@/store/useBrowserProfileCreateStore";
import { CopyText } from "@/routes/workflows/editor/Workspace";
import { type BrowserSession as BrowserSessionType } from "@/routes/workflows/types/browserSessionTypes";
import {
  resolveStreamTransport,
  useBrowserStreamingMode,
} from "@/hooks/useRuntimeConfig";
import {
  StreamModeBadge,
  type StreamMode,
} from "@/routes/streaming/StreamDiagnostics";

import { getBrowserSessionRefetchIntervalMs } from "./browserSessionQueryUtils";
import { BrowserSessionDownloads } from "./BrowserSessionDownloads";
import { BrowserSessionOccupiedBy } from "./BrowserSessionOccupiedBy";
import { BrowserSessionVideo } from "./BrowserSessionVideo";
import { BrowserSessionStream } from "./BrowserSessionStream";
import { BrowserSessionTimeline } from "./BrowserSessionTimeline";
import { BrowserSessionWorkflowRuns } from "./BrowserSessionWorkflowRuns";
import {
  getBrowserSessionTabFromPathname,
  getSessionControlsState,
} from "./BrowserSession.utils";

// PersistentBrowserSessionStatus values (skyvern/forge/sdk/schemas/persistent_browser_sessions.py)
const BROWSER_SESSION_STATUS_BADGE_VARIANT: Record<
  string,
  "success" | "warning" | "destructive" | "secondary"
> = {
  completed: "success",
  failed: "destructive",
  timeout: "destructive",
  running: "warning",
  retry: "warning",
  created: "secondary",
};

const TAB_OPTIONS = [
  { label: "Stream", to: "stream" },
  { label: "Recordings", to: "recordings" },
  { label: "Downloads", to: "downloads" },
  { label: "Timeline", to: "timeline" },
  { label: "Runs", to: "runs" },
];

function BrowserTabStrip({
  options,
}: {
  options: { label: string; to: string }[];
}) {
  const [searchParams] = useSearchParams();
  return (
    // Scrolls rather than clips: on a short pane the window narrows to the preview,
    // and the five tabs plus the docked controls stop fitting around ~540px.
    <div className="flex min-w-0 items-end gap-0.5 overflow-x-auto">
      {options.map((option) => (
        <NavLink
          key={option.to}
          to={`${option.to}?${searchParams.toString()}`}
          replace
          className={({ isActive }) =>
            cn(
              "flex-shrink-0 rounded-t-md px-3 py-1.5 text-sm",
              "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-slate-500",
              isActive
                ? // Same surface as the toolbar directly beneath, so the active tab
                  // reads as fused to the window rather than sitting on top of it.
                  "bg-slate-800 text-foreground"
                : "text-slate-400 hover:bg-slate-800/50 hover:text-foreground",
            )
          }
        >
          {option.label}
        </NavLink>
      ))}
    </div>
  );
}

function BrowserSession() {
  const { browserSessionId } = useParams();
  const location = useLocation();
  const activeTab = getBrowserSessionTabFromPathname(location.pathname);
  const [isDialogOpen, setIsDialogOpen] = useState(false);
  const [isSaveProfileDialogOpen, setIsSaveProfileDialogOpen] = useState(false);
  const [vncFailed, setVncFailed] = useState(false);
  const [previewWidth, setPreviewWidth] = useState<number | null>(null);
  const { browserStreamingMode } = useBrowserStreamingMode();

  // Only the Stream tab has a preview to size the window against; the other tabs
  // hold tables and want the whole pane.
  const windowWidth = activeTab === "stream" ? previewWidth : null;

  useEffect(() => {
    setVncFailed(false);
  }, [browserSessionId]);

  const credentialGetter = useCredentialGetter();
  const { startBackgroundCreate } = useBackgroundBrowserProfileCreate();
  const activeProfileCreate = useBrowserProfileCreateStore(
    (state) => state.active,
  );

  const query = useQuery({
    queryKey: ["browserSession", browserSessionId],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const response = await client.get<BrowserSessionType>(
        `/browser_sessions/${browserSessionId}`,
      );
      return response.data;
    },
    refetchInterval: (query) =>
      getBrowserSessionRefetchIntervalMs(query.state.data),
  });

  const browserSession = query.data;
  const { showControls, isSavingProfile, showCloseSession } =
    getSessionControlsState({
      browserSessionId,
      status: browserSession?.status,
      savingProfileSessionId: activeProfileCreate?.browserSessionId,
    });
  const isCdpMode =
    resolveStreamTransport(
      browserStreamingMode,
      browserSession?.stream_transport,
    ) === "cdp";
  const streamMode: StreamMode = isCdpMode
    ? "cdp"
    : browserSession?.vnc_streaming_supported
      ? vncFailed
        ? "fallback"
        : "vnc"
      : "unavailable";

  const closeBrowserSessionMutation = useCloseBrowserSessionMutation({
    browserSessionId,
    onSuccess: () => {
      setIsDialogOpen(false);
    },
  });

  if (query.isLoading) {
    return (
      <div className="h-screen w-full gap-4 p-6">
        <div className="flex h-full w-full items-center justify-center gap-2 text-muted-foreground">
          <ReloadIcon className="h-4 w-4 animate-spin" />
          Loading browser session...
        </div>
      </div>
    );
  }

  if (query.isError || !browserSession) {
    return (
      <div className="h-screen w-full gap-4 p-6">
        <div className="flex h-full w-full items-center justify-center">
          {/* we need nice artwork here */}
          No browser session found.
        </div>
      </div>
    );
  }

  return (
    <div className="h-screen w-full gap-4 p-6">
      {/* One window: the tab strip is its title bar, the stream's own toolbar sits
          directly beneath, and both track the preview's width on the Stream tab. */}
      <div
        className={cn(
          "mx-auto flex h-full min-w-0 max-w-full flex-col items-stretch justify-start",
          // Switching off Stream snaps the window from the preview's width to the
          // full pane; easing it reads as the window opening out rather than a jump.
          "transition-[width] duration-200 ease-out motion-reduce:transition-none",
        )}
        style={windowWidth ? { width: windowWidth } : undefined}
      >
        <div className="flex w-full flex-shrink-0 flex-row items-center justify-between p-4">
          <div className="flex w-full flex-row items-center justify-start gap-2">
            <div className="text-xl">Browser Session</div>
            {activeTab === "stream" && <StreamModeBadge mode={streamMode} />}
            {browserSession && (
              <div className="ml-auto flex flex-col items-end justify-end overflow-hidden">
                <div className="flex items-center justify-end gap-2">
                  <Badge
                    variant={
                      BROWSER_SESSION_STATUS_BADGE_VARIANT[
                        browserSession.status
                      ] ?? "secondary"
                    }
                  >
                    {browserSession.status}
                  </Badge>
                  <div className="max-w-[20rem] truncate font-mono text-xs opacity-75">
                    {browserSession.browser_session_id}
                  </div>
                  <CopyText
                    className="opacity-75 hover:opacity-100"
                    text={browserSession.browser_session_id}
                  />
                </div>
                {browserSession.browser_address && (
                  <div className="flex items-center justify-end">
                    <div className="max-w-[20rem] truncate font-mono text-xs opacity-75">
                      {browserSession.browser_address}
                    </div>
                    <CopyText
                      className="opacity-75 hover:opacity-100"
                      text={browserSession.browser_address}
                    />
                  </div>
                )}
                {browserSession.runnable_id && (
                  <BrowserSessionOccupiedBy
                    runnableId={browserSession.runnable_id}
                  />
                )}
              </div>
            )}
          </div>
        </div>

        {/* Title bar: section tabs left, session controls docked right. Controls come
            after every tab in the DOM so keyboard order stays tabs-then-actions. */}
        <div className="flex w-full flex-shrink-0 items-center gap-2 rounded-t-lg border border-b-0 bg-slate-900 px-2 pt-1">
          <BrowserTabStrip options={TAB_OPTIONS} />

          {showControls && (
            <div className="ml-auto flex flex-shrink-0 items-center gap-1 pb-1">
              <Button
                variant="ghost"
                size="sm"
                className={cn(
                  "h-7 px-2 text-xs text-slate-300",
                  isSavingProfile
                    ? "cursor-not-allowed"
                    : "hover:text-foreground",
                )}
                onClick={() => {
                  if (isSavingProfile) {
                    return;
                  }
                  setIsSaveProfileDialogOpen(true);
                }}
                // `disabled` would pull in the button base's
                // disabled:pointer-events-none (killing the title tooltip) and
                // disabled:opacity-50 (dimming the only in-progress feedback to
                // 3.9:1, under the 4.5:1 AA floor).
                aria-disabled={isSavingProfile}
                title={
                  isSavingProfile ? "Saving profile — please wait" : undefined
                }
              >
                {isSavingProfile ? (
                  <ReloadIcon className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                ) : (
                  <BrowserIcon className="mr-1.5 h-3.5 w-3.5" />
                )}
                {isSavingProfile ? "Saving…" : "Save Profile"}
              </Button>
              {showCloseSession && (
                <Dialog open={isDialogOpen} onOpenChange={setIsDialogOpen}>
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <DialogTrigger asChild>
                          <Button
                            variant="ghost"
                            size="icon"
                            aria-label="Close browser session"
                            className="h-7 w-7 text-slate-300 hover:bg-red-500/15 hover:text-red-300"
                          >
                            <Cross2Icon className="h-3.5 w-3.5" />
                          </Button>
                        </DialogTrigger>
                      </TooltipTrigger>
                      <TooltipContent>Close</TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                  <DialogContent>
                    <DialogHeader>
                      <DialogTitle>Are you sure?</DialogTitle>
                      <DialogDescription>
                        Are you sure you want to stop (shut down) this browser
                        session?
                      </DialogDescription>
                    </DialogHeader>
                    <DialogFooter>
                      <DialogClose asChild>
                        <Button variant="secondary">Back</Button>
                      </DialogClose>
                      <Button
                        variant="destructive"
                        onClick={() => {
                          closeBrowserSessionMutation.mutate();
                        }}
                        disabled={closeBrowserSessionMutation.isPending}
                      >
                        {closeBrowserSessionMutation.isPending && (
                          <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />
                        )}
                        Stop Browser Session
                      </Button>
                    </DialogFooter>
                  </DialogContent>
                </Dialog>
              )}
            </div>
          )}
        </div>

        {/* No top border: the active tab shares the toolbar's surface, so a seam here
            would cut the tab off from the window it belongs to. */}
        <div className="relative min-h-0 w-full flex-1 overflow-hidden rounded-b-lg border border-t-0">
          <div
            className="absolute left-0 top-0 z-10 flex h-full w-full items-center justify-center"
            style={{
              visibility: activeTab === "stream" ? "visible" : "hidden",
              pointerEvents: activeTab === "stream" ? "auto" : "none",
            }}
          >
            {(isCdpMode || vncFailed) && browserSessionId && (
              <BrowserSessionStream
                browserSessionId={browserSessionId}
                interactive={true}
                showControlButtons={true}
                enableUrlInput={true}
                forceCdp={vncFailed}
                onFrameWidthChange={setPreviewWidth}
              />
            )}
            {!isCdpMode &&
              browserSession.vnc_streaming_supported &&
              !vncFailed && (
                <BrowserStream
                  browserSessionId={browserSessionId}
                  interactive={false}
                  showControlButtons={true}
                  isVisible={activeTab === "stream"}
                  onClose={() => setVncFailed(true)}
                />
              )}
          </div>
          <div
            className="absolute left-0 top-0 h-full w-full"
            style={{
              visibility: activeTab === "recordings" ? "visible" : "hidden",
              pointerEvents: activeTab === "recordings" ? "auto" : "none",
            }}
          >
            <BrowserSessionVideo
              browserSession={browserSession}
              refreshBrowserSession={async () => (await query.refetch()).data}
            />
          </div>
          <div
            className="absolute left-0 top-0 h-full w-full"
            style={{
              visibility: activeTab === "downloads" ? "visible" : "hidden",
              pointerEvents: activeTab === "downloads" ? "auto" : "none",
            }}
          >
            <BrowserSessionDownloads />
          </div>
          <div
            className="absolute left-0 top-0 h-full w-full"
            style={{
              visibility: activeTab === "timeline" ? "visible" : "hidden",
              pointerEvents: activeTab === "timeline" ? "auto" : "none",
            }}
          >
            <BrowserSessionTimeline />
          </div>
          <div
            className="absolute left-0 top-0 h-full w-full overflow-auto p-1"
            style={{
              visibility: activeTab === "runs" ? "visible" : "hidden",
              pointerEvents: activeTab === "runs" ? "auto" : "none",
            }}
          >
            <BrowserSessionWorkflowRuns />
          </div>
        </div>
      </div>
      <Outlet />
      <Toaster />
      {browserSessionId && (
        <SaveSessionAsBrowserProfileDialog
          browserSessionId={browserSessionId}
          isSessionRunning={browserSession?.status === "running"}
          onStartBackgroundCreate={startBackgroundCreate}
          open={isSaveProfileDialogOpen}
          onOpenChange={setIsSaveProfileDialogOpen}
        />
      )}
    </div>
  );
}

export { BrowserSession };
