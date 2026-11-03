import { getClient } from "@/api/AxiosClient";
import { Status as WorkflowRunStatus } from "@/api/types";
import { Button } from "@/components/ui/button";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { cn } from "@/util/utils";
import { HandIcon } from "@radix-ui/react-icons";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useLayoutEffect, useRef, useState } from "react";

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
  // Actionable only while this block's own run is paused and the block is still
  // running — else a historical prompt would resolve the wrong pause.
  const isAwaitingInteraction =
    workflowRun?.status === WorkflowRunStatus.Paused &&
    workflowRunBlock.status === WorkflowRunStatus.Running;

  const positiveLabel = workflowRunBlock.positive_descriptor || "Approve";
  const negativeLabel = workflowRunBlock.negative_descriptor || "Reject";
  const instructions =
    workflowRunBlock.instructions ||
    "The agent is paused and waiting for your review.";

  const [isDialogOpen, setIsDialogOpen] = useState(false);
  const [choice, setChoice] = useState<"approve" | "reject" | null>(null);

  const [expanded, setExpanded] = useState(false);
  const [isCutOff, setIsCutOff] = useState(false);
  const instructionsRef = useRef<HTMLParagraphElement>(null);

  // The toggle only means something when the clamp is actually hiding text, so
  // measure instead of guessing from the length.
  useLayoutEffect(() => {
    const element = instructionsRef.current;
    if (!isAwaitingInteraction || !element || expanded) {
      return;
    }
    const measure = () =>
      setIsCutOff(element.scrollHeight > element.clientHeight + 1);
    measure();
    if (typeof ResizeObserver === "undefined") {
      return;
    }
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [isAwaitingInteraction, expanded, instructions]);

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
    <section
      aria-label="Action needed"
      className="flex shrink-0 flex-col gap-3 rounded-lg border border-amber-500/40 bg-amber-500/10 p-3"
    >
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

      <div className="flex items-center gap-2">
        <HandIcon className="size-5 shrink-0 text-amber-600 dark:text-amber-400" />
        <span className="text-sm font-semibold">Your action is needed</span>
        {workflowRunBlock.label ? (
          <span className="ml-auto truncate font-mono text-xs text-muted-foreground">
            {workflowRunBlock.label}
          </span>
        ) : null}
      </div>
      <div className="flex flex-col items-start gap-1">
        <p
          ref={instructionsRef}
          id={`${workflowRunBlock.workflow_run_block_id}-instructions`}
          className={cn("w-full whitespace-pre-line break-words text-sm", {
            "line-clamp-3": !expanded,
            "max-h-[8.75rem] overflow-y-auto overscroll-contain pr-1": expanded,
          })}
        >
          {instructions}
        </p>
        {isCutOff || expanded ? (
          <button
            type="button"
            aria-expanded={expanded}
            aria-controls={`${workflowRunBlock.workflow_run_block_id}-instructions`}
            className="text-xs font-semibold text-amber-700 underline underline-offset-2 hover:text-amber-600 dark:text-amber-400 dark:hover:text-amber-300"
            onClick={() => setExpanded((value) => !value)}
          >
            {expanded ? "Show less" : "Show more"}
          </button>
        ) : null}
      </div>
      <div className="flex gap-2">
        <Button
          variant="outline"
          className="h-auto min-h-9 flex-1 whitespace-normal border-red-500/40 bg-transparent py-2 text-red-600 hover:bg-red-500/10 hover:text-red-600 dark:text-red-400 dark:hover:text-red-400"
          onClick={() => {
            setChoice("reject");
            setIsDialogOpen(true);
          }}
        >
          {negativeLabel}
        </Button>
        <Button
          variant="default"
          className="h-auto min-h-9 flex-1 whitespace-normal py-2"
          onClick={() => {
            setChoice("approve");
            setIsDialogOpen(true);
          }}
        >
          {positiveLabel}
        </Button>
      </div>
    </section>
  );
}
