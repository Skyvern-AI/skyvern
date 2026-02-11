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
  const { data: workflowRun } = useWorkflowRunWithWorkflowQuery();
  const isPaused =
    workflowRun && workflowRun.status === WorkflowRunStatus.Paused;

  const buttonLayout =
    (workflowRunBlock.positive_descriptor?.length ?? 0) < 8 &&
    (workflowRunBlock.negative_descriptor?.length ?? 0) < 8
      ? "inline"
      : "stacked";

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
        title: `${workflowRunBlock.positive_descriptor}`,
        description: `Successfully chose: ${workflowRunBlock.positive_descriptor}`,
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
        title: `${workflowRunBlock.negative_descriptor}`,
        description: `Successfully chose: ${workflowRunBlock.negative_descriptor}`,
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

  if (!isPaused) {
    return null;
  }

  return (
    <div className="mt-4 flex flex-col gap-4 rounded-md bg-slate-elevation4 p-4">
      <Dialog open={isDialogOpen} onOpenChange={setIsDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {choice === "approve"
                ? workflowRunBlock.positive_descriptor
                : workflowRunBlock.negative_descriptor}
            </DialogTitle>
            <DialogDescription>Are you sure?</DialogDescription>
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

      <div className="text-sm">{workflowRunBlock.instructions}</div>
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
          <div>{workflowRunBlock.negative_descriptor}</div>
        </Button>
        <Button
          variant="default"
          onClick={() => {
            setChoice("approve");
            setIsDialogOpen(true);
          }}
        >
          <div>{workflowRunBlock.positive_descriptor}</div>
        </Button>
      </div>
    </div>
  );
}
