import { ReloadIcon, StopIcon } from "@radix-ui/react-icons";
import { useEffect, useState } from "react";
import { Outlet, useLocation, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { getClient } from "@/api/AxiosClient";
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
import { LogoMinimized } from "@/components/LogoMinimized";
import { SwitchBarNavigation } from "@/components/SwitchBarNavigation";
import { Toaster } from "@/components/ui/toaster";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useCloseBrowserSessionMutation } from "@/routes/browserSessions/hooks/useCloseBrowserSessionMutation";
import { SaveSessionAsBrowserProfileDialog } from "@/routes/browserProfiles/SaveSessionAsBrowserProfileDialog";
import { useBackgroundBrowserProfileCreate } from "@/routes/browserProfiles/hooks/useBackgroundBrowserProfileCreate";
import { CopyText } from "@/routes/workflows/editor/Workspace";
import { type BrowserSession as BrowserSessionType } from "@/routes/workflows/types/browserSessionTypes";
import { useBrowserStreamingMode } from "@/hooks/useRuntimeConfig";
import {
  StreamModeBadge,
  type StreamMode,
} from "@/routes/streaming/StreamDiagnostics";

import { getBrowserSessionRefetchIntervalMs } from "./browserSessionQueryUtils";
import { BrowserSessionDownloads } from "./BrowserSessionDownloads";
import { BrowserSessionVideo } from "./BrowserSessionVideo";
import { BrowserSessionStream } from "./BrowserSessionStream";
import { BrowserSessionWorkflowRuns } from "./BrowserSessionWorkflowRuns";

type TabName = "stream" | "recordings" | "downloads" | "runs";

function BrowserSession() {
  const { browserSessionId } = useParams();
  const location = useLocation();
  const activeTab: TabName = location.pathname.endsWith("/recordings")
    ? "recordings"
    : location.pathname.endsWith("/downloads")
      ? "downloads"
      : location.pathname.endsWith("/runs")
        ? "runs"
        : "stream";
  const [isDialogOpen, setIsDialogOpen] = useState(false);
  const [isSaveProfileDialogOpen, setIsSaveProfileDialogOpen] = useState(false);
  const [vncFailed, setVncFailed] = useState(false);
  const { browserStreamingMode } = useBrowserStreamingMode();
  const isCdpMode = browserStreamingMode === "cdp";

  useEffect(() => {
    setVncFailed(false);
  }, [browserSessionId]);

  const credentialGetter = useCredentialGetter();
  const { startBackgroundCreate } = useBackgroundBrowserProfileCreate();

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
        <div className="flex h-full w-full items-center justify-center">
          {/* we need nice artwork here */}
          Loading...
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
      <div className="flex h-full w-full flex-col items-start justify-start gap-2">
        <div className="flex w-full flex-shrink-0 flex-row items-center justify-between rounded-lg border p-4">
          <div className="flex w-full flex-row items-center justify-start gap-2">
            <LogoMinimized />
            <div className="text-xl">Browser Session</div>
            {activeTab === "stream" && <StreamModeBadge mode={streamMode} />}
            {browserSession && (
              <div className="ml-auto flex flex-col items-end justify-end overflow-hidden">
                <div className="flex items-center justify-end gap-2">
                  <span
                    className={`rounded px-2 py-0.5 text-xs font-medium ${
                      browserSession.status === "running"
                        ? "bg-green-500/20 text-green-500"
                        : browserSession.status === "completed"
                          ? "bg-blue-500/20 text-blue-500"
                          : browserSession.status === "failed"
                            ? "bg-red-500/20 text-red-500"
                            : "bg-gray-500/20 text-gray-500"
                    }`}
                  >
                    {browserSession.status}
                  </span>
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
              </div>
            )}
          </div>
        </div>

        {/* Tab Navigation */}
        <div className="flex w-full items-center justify-start gap-2">
          <SwitchBarNavigation
            options={[
              { label: "Stream", to: "stream" },
              { label: "Recordings", to: "recordings" },
              { label: "Downloads", to: "downloads" },
              { label: "Runs", to: "runs" },
            ]}
          />

          {browserSessionId && browserSession?.status === "running" && (
            <div className="ml-auto flex items-center gap-2">
              <Button
                variant="default"
                onClick={() => setIsSaveProfileDialogOpen(true)}
              >
                <BrowserIcon className="mr-2 h-4 w-4" />
                Save Profile
              </Button>
              <Dialog open={isDialogOpen} onOpenChange={setIsDialogOpen}>
                <DialogTrigger asChild>
                  <Button variant="ghost">
                    <StopIcon className="mr-2 h-4 w-4" />
                    Stop
                  </Button>
                </DialogTrigger>
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
            </div>
          )}
        </div>

        {/* Tab Content */}
        <div className="relative min-h-0 w-full flex-1 rounded-lg border p-4">
          <div
            className="absolute left-0 top-0 z-10 flex h-full w-full items-center justify-center"
            style={{
              visibility: activeTab === "stream" ? "visible" : "hidden",
              pointerEvents: activeTab === "stream" ? "auto" : "none",
            }}
          >
            {isCdpMode && browserSessionId && (
              <BrowserSessionStream
                browserSessionId={browserSessionId}
                interactive={true}
                showControlButtons={true}
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
            <BrowserSessionVideo />
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
