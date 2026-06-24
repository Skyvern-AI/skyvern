import {
  GlobeIcon,
  OpenInNewWindowIcon,
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
import {
  StreamModeBadge,
  StreamStatusPanel,
} from "@/routes/streaming/StreamDiagnostics";
import { useStudioBrowserStore } from "@/store/useStudioBrowserStore";

import { useDebugSessionQuery } from "../hooks/useDebugSessionQuery";
import { useStudioShellContext } from "./StudioShellContext";

const ICON_BUTTON =
  "shrink-0 rounded p-1.5 text-muted-foreground hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-40";

/**
 * Browser tab of the studio shell — the persistent debug browser. The stream node
 * lives in the shell and is re-parented into this tab's slot.
 */
export function BrowserTab() {
  const { workflowPermanentId } = useParams();
  const { setBrowserStreamSlot } = useStudioShellContext();
  const { browserStreamingMode } = useBrowserStreamingMode();
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const { data: debugSession } = useDebugSessionQuery({
    workflowPermanentId,
    enabled: false,
  });
  const browserSessionId = debugSession?.browser_session_id ?? null;

  const streamUrl = useStudioBrowserStore((s) => s.streamUrl);
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
    <div className="flex h-full min-h-0 w-full flex-col gap-3 p-3">
      <div className="flex shrink-0 items-center gap-1.5 rounded-lg border border-border bg-slate-elevation1 px-3 py-2">
        <GlobeIcon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        <span className="min-w-0 flex-1 truncate text-sm font-medium text-foreground">
          {streamUrl || "Live browser"}
        </span>
        <button
          type="button"
          title="Reconnect"
          aria-label="Reconnect browser stream"
          onClick={reload}
          disabled={!browserSessionId}
          className={ICON_BUTTON}
        >
          <ReloadIcon className="h-4 w-4" />
        </button>
        <button
          type="button"
          title="Open in new tab"
          aria-label="Open browser in new tab"
          onClick={openInNewTab}
          disabled={!browserSessionId}
          className={ICON_BUTTON}
        >
          <OpenInNewWindowIcon className="h-4 w-4" />
        </button>
        <StreamModeBadge mode={browserStreamingMode} className="shrink-0" />
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
              title="Turn off browser"
              aria-label="Turn off browser"
              disabled={!workflowPermanentId || !browserSessionId}
              className={ICON_BUTTON}
            >
              <PowerIcon className="h-4 w-4" />
            </button>
          </DialogTrigger>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Turn off this browser?</DialogTitle>
              <DialogDescription>
                This ends the current browser and starts a fresh one. Anything
                in progress here will stop.
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
                  workflowPermanentId &&
                  cycleBrowser.mutate(workflowPermanentId)
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
      </div>

      <div className="relative flex min-h-0 flex-1 items-center justify-center overflow-hidden rounded-lg border border-border bg-slate-950">
        {browserSessionId ? (
          <div ref={setBrowserStreamSlot} className="absolute inset-0" />
        ) : (
          <StreamStatusPanel
            diagnostic={{
              title: "Warming up your browser",
              detail:
                "Spinning up the debug browser — this only takes a moment.",
              pending: true,
            }}
          />
        )}
      </div>
    </div>
  );
}
