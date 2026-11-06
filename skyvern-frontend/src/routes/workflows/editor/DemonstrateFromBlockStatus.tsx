import { ReloadIcon } from "@radix-ui/react-icons";
import { useEffect, useState } from "react";

import { getClient } from "@/api/AxiosClient";
import { Status } from "@/api/types";
import { Button } from "@/components/ui/button";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useWorkflowPermanentId } from "@/routes/workflows/WorkflowPermanentIdContext";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import {
  demonstrateFromBlockStep,
  useDemonstrateFromBlockStore,
} from "@/store/useDemonstrateFromBlockStore";
import { useRecordingLauncherStore } from "@/store/useRecordingLauncherStore";

// Follows the run of the earlier blocks and hands the browser to the user once it lands.
function DemonstrateFromBlockStatus() {
  const workflowPermanentId = useWorkflowPermanentId();
  const storedPending = useDemonstrateFromBlockStore((state) => state.pending);
  const setPending = useDemonstrateFromBlockStore((state) => state.setPending);
  // The recording launcher belongs to the agent on screen, so another agent's pending demonstration never starts here.
  const pending =
    storedPending?.workflowPermanentId === workflowPermanentId
      ? storedPending
      : null;
  const startDemonstration = useRecordingLauncherStore(
    (state) => state.startDemonstration,
  );
  const credentialGetter = useCredentialGetter();
  const [cancelling, setCancelling] = useState(false);
  const { data: run } = useWorkflowRunQuery({
    workflowRunId: pending?.runId,
  });
  const step = demonstrateFromBlockStep(run?.status);

  useEffect(() => {
    if (storedPending && !pending) setPending(null);
  }, [pending, setPending, storedPending]);

  // Leaving the editor drops the handoff; the run itself keeps going.
  useEffect(() => () => setPending(null), [setPending]);

  useEffect(() => {
    if (!pending || step !== "start" || !startDemonstration || cancelling)
      return;
    setPending(null);
    startDemonstration({ insertion: pending.insertion });
  }, [cancelling, pending, setPending, startDemonstration, step]);

  if (!pending) return null;

  const cancel = async () => {
    if (step !== "running") {
      setPending(null);
      return;
    }
    if (!pending.browserSessionId) {
      setPending(null);
      toast({
        title: "The earlier blocks are still running",
        description:
          "Skyvern stopped waiting to hand you the browser. Stop the run from its run page if you need to.",
      });
      return;
    }
    setCancelling(true);
    try {
      const client = await getClient(credentialGetter);
      await client.post(
        `/runs/${pending.browserSessionId}/workflow_run/${pending.runId}/cancel/`,
      );
      setPending(null);
    } catch {
      toast({
        variant: "destructive",
        title: "Couldn't cancel the run",
        description: "Try Cancel again.",
      });
    } finally {
      setCancelling(false);
    }
  };

  return (
    <div
      role="status"
      className="fixed bottom-6 left-1/2 z-50 w-[440px] max-w-[calc(100vw-2rem)] -translate-x-1/2 rounded-lg border border-slate-700 bg-slate-elevation3 p-4 shadow-lg"
    >
      {step === "failed" ? (
        <>
          <p className="text-sm font-medium">
            {run?.status === Status.Canceled
              ? `The run was stopped before ${pending.blockLabel}`
              : `Couldn't reach ${pending.blockLabel}`}
          </p>
          <p className="mt-1 line-clamp-3 text-xs text-slate-400">
            {run?.failure_reason ??
              "A block before it didn't finish, so the browser isn't there yet."}
          </p>
          <div className="mt-3 flex justify-end gap-2">
            <Button size="sm" variant="ghost" onClick={() => setPending(null)}>
              Cancel
            </Button>
            <Button
              size="sm"
              variant="secondary"
              disabled={!startDemonstration}
              onClick={() => {
                setPending(null);
                startDemonstration?.({ insertion: pending.insertion });
              }}
            >
              Record from here
            </Button>
            <Button
              size="sm"
              onClick={() => {
                setPending(null);
                pending.retry();
              }}
            >
              Retry
            </Button>
          </div>
        </>
      ) : (
        <div className="flex items-start gap-3">
          <ReloadIcon className="mt-0.5 size-4 shrink-0 animate-spin text-slate-400" />
          <div className="min-w-0 flex-1">
            <p className="text-sm font-medium">
              {step === "start"
                ? "Handing you the browser"
                : `Getting the browser to ${pending.blockLabel}`}
            </p>
            <p className="mt-1 text-xs text-slate-400">
              {step === "start" && !startDemonstration
                ? "Recording starts once Copilot and the browser are ready."
                : `Running ${pending.prefixLabels.join(", ")}. You'll demonstrate ${pending.blockLabel} when they finish.`}
            </p>
          </div>
          <Button
            size="sm"
            variant="ghost"
            disabled={cancelling}
            onClick={() => void cancel()}
          >
            Cancel
          </Button>
        </div>
      )}
    </div>
  );
}

export { DemonstrateFromBlockStatus };
