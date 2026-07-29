import { getClient } from "@/api/AxiosClient";
import { Status as WorkflowRunStatus } from "@/api/types";
import { Button } from "@/components/ui/button";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { cn } from "@/util/utils";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { toast } from "@/components/ui/use-toast";
import { useWorkflowRunWithWorkflowQuery } from "../hooks/useWorkflowRunWithWorkflowQuery";
import { WorkflowRunBlock } from "../types/workflowRunTypes";

interface Props {
  workflowRunBlock: WorkflowRunBlock;
}

export function WorkflowRunHumanInteraction({ workflowRunBlock }: Props) {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  // The studio run view carries the run id in a query param, not a route
  // param, so resolve the run from the block itself rather than the URL.
  const { data: workflowRun } = useWorkflowRunWithWorkflowQuery({
    workflowRunId: workflowRunBlock.workflow_run_id,
  });
  // Actionable only when the resolved run IS this block's run (keepPreviousData can
  // briefly return the prior run while switching), the run is paused, and this block
  // is still running — else a stale/historical prompt would resolve the wrong pause
  // (the continue/cancel mutations target the resolved run).
  const isAwaitingInteraction =
    workflowRun?.workflow_run_id === workflowRunBlock.workflow_run_id &&
    workflowRun?.status === WorkflowRunStatus.Paused &&
    workflowRunBlock.status === WorkflowRunStatus.Running;

  const positiveLabel = workflowRunBlock.positive_descriptor || "Approve";
  const negativeLabel = workflowRunBlock.negative_descriptor || "Reject";

  const buttonLayout =
    positiveLabel.length < 8 && negativeLabel.length < 8 ? "inline" : "stacked";

  const [isDialogOpen, setIsDialogOpen] = useState(false);
  const [choice, setChoice] = useState<"approve" | "reject" | null>(null);

  const approveMutation = useMutation({
    mutationFn: async () => {
      if (!workflowRun) {
        return;
      }

      const client = await getClient(credentialGetter, "sans-api-v1");

      return await client.post(
        `/workflows/runs/${workflowRun.workflow_run_id}/continue`,
      );
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["workflowRun"],
      });

      toast({
        variant: "success",
        title: positiveLabel,
        description: `Successfully chose: ${positiveLabel}`,
      });
    },
    onError: (error) => {
      toast({
        variant: "destructive",
        title: "Interaction Failed",
        description: error.message,
      });
    },
  });

  const rejectMutation = useMutation({
    mutationFn: async () => {
      if (!workflowRun) {
        return;
      }

      const client = await getClient(credentialGetter);

      return await client.post(
        `/workflows/runs/${workflowRun.workflow_run_id}/cancel`,
      );
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["workflowRun"],
      });

      toast({
        variant: "success",
        title: negativeLabel,
        description: `Successfully chose: ${negativeLabel}`,
      });
    },
    onError: (error) => {
      toast({
        variant: "destructive",
        title: "Interaction Failed",
        description: error.message,
      });
    },
  });

  if (!isAwaitingInteraction) {
    return null;
  }

  return (
    <div className="mt-4 flex flex-col gap-4 rounded-md bg-slate-elevation4 p-4">
      <Dialog open={isDialogOpen} onOpenChange={setIsDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {choice === "approve" ? positiveLabel : negativeLabel}
            </DialogTitle>
            <DialogDescription>
              {choice === "approve"
                ? "The agent will continue running from where it paused."
                : "The agent run will be stopped and can't be resumed."}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <DialogClose asChild>
              <Button variant="secondary">Back</Button>
            </DialogClose>
            <Button
              variant={choice === "reject" ? "destructive" : "default"}
              onClick={() => {
                if (choice === "approve") {
                  approveMutation.mutate();
                } else if (choice === "reject") {
                  rejectMutation.mutate();
                }
              }}
            >
              Proceed
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <div className="text-sm">
        {workflowRunBlock.instructions ||
          "The agent is paused and waiting for your review."}
      </div>
      <div
        className={cn("flex gap-2", {
          "justify-between": buttonLayout === "inline",
          "flex-col": buttonLayout === "stacked",
        })}
      >
        <Button
          variant="destructive"
          onClick={() => {
            setChoice("reject");
            setIsDialogOpen(true);
          }}
        >
          <div>{negativeLabel}</div>
        </Button>
        <Button
          variant="default"
          onClick={() => {
            setChoice("approve");
            setIsDialogOpen(true);
          }}
        >
          <div>{positiveLabel}</div>
        </Button>
      </div>
    </div>
  );
}
