import { ReloadIcon, StopIcon } from "@radix-ui/react-icons";
import { useState } from "react";
import { useParams } from "react-router-dom";
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
import { LogoMinimized } from "@/components/LogoMinimized";
import { SwitchBar } from "@/components/SwitchBar";
import { Toaster } from "@/components/ui/toaster";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useCloseBrowserSessionMutation } from "@/routes/browserSessions/hooks/useCloseBrowserSessionMutation";
import { CopyText } from "@/routes/workflows/editor/Workspace";
import { type BrowserSession as BrowserSessionType } from "@/routes/workflows/types/browserSessionTypes";

import { BrowserSessionVideo } from "./BrowserSessionVideo";

type TabName = "stream" | "videos";

function BrowserSession() {
  const { browserSessionId } = useParams();
  const [hasBrowserSession, setHasBrowserSession] = useState(false);
  const [browserSession, setBrowserSession] =
    useState<BrowserSessionType | null>(null);
  const [activeTab, setActiveTab] = useState<TabName>("stream");
  const [isDialogOpen, setIsDialogOpen] = useState(false);

  const credentialGetter = useCredentialGetter();

  const query = useQuery({
    queryKey: ["browserSession", browserSessionId],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");

      try {
        const response = await client.get<BrowserSessionType>(
          `/browser_sessions/${browserSessionId}`,
        );
        setHasBrowserSession(true);
        setBrowserSession(response.data);
        return response.data;
      } catch (error) {
        setHasBrowserSession(false);
        setBrowserSession(null);
        return null;
      }
    },
  });

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

  if (!hasBrowserSession) {
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
            {browserSession && (
              <div className="ml-auto flex flex-col items-end justify-end overflow-hidden">
                <div className="flex items-center justify-end">
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
          <SwitchBar
            className="border-none"
            onChange={(value) => setActiveTab(value as TabName)}
            value={activeTab}
            options={[
              {
                label: "Stream",
                value: "stream",
                helpText: "The live stream of the browser session (if active).",
              },
              {
                label: "Recordings",
                value: "videos",
                helpText: "All recordings of this browser session.",
              },
            ]}
          />

          {browserSessionId && (
            <Dialog open={isDialogOpen} onOpenChange={setIsDialogOpen}>
              <DialogTrigger asChild>
                <Button className="ml-auto" variant="secondary">
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
            <BrowserStream
              browserSessionId={browserSessionId}
              interactive={false}
              showControlButtons={true}
              isVisible={activeTab === "stream"}
            />
          </div>
          <div
            className="h-full w-full"
            style={{
              visibility: activeTab === "videos" ? "visible" : "hidden",
              pointerEvents: activeTab === "videos" ? "auto" : "none",
            }}
          >
            <BrowserSessionVideo />
          </div>
        </div>
      </div>
      <Toaster />
    </div>
  );
}

export { BrowserSession };
