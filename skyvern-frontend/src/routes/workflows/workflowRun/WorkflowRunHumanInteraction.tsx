import { getClient } from "@/api/AxiosClient";
import { Status as WorkflowRunStatus } from "@/api/types";
import { Button } from "@/components/ui/button";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { cn } from "@/util/utils";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { toast } from "@/components/ui/use-toast";
import { useWorkflowRunQuery } from "../hooks/useWorkflowRunQuery";
import { HumanInteractionBlock } from "../types/workflowTypes";

interface Props {
  humanInteractionBlock: HumanInteractionBlock;
}

export function WorkflowRunHumanInteraction({ humanInteractionBlock }: Props) {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const { data: workflowRun } = useWorkflowRunQuery();
  const isPaused =
    workflowRun && workflowRun.status === WorkflowRunStatus.Paused;

  const buttonLayout =
    humanInteractionBlock.positive_descriptor.length < 8 &&
    humanInteractionBlock.negative_descriptor.length < 8
      ? "inline"
      : "stacked";

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
        title: `${humanInteractionBlock.positive_descriptor}`,
        description: `Successfully chose: ${humanInteractionBlock.positive_descriptor}`,
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
        title: `${humanInteractionBlock.negative_descriptor}`,
        description: `Successfully chose: ${humanInteractionBlock.negative_descriptor}`,
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
      <div className="text-sm">{humanInteractionBlock.instructions}</div>
      <div
        className={cn("flex gap-2", {
          "justify-between": buttonLayout === "inline",
          "flex-col": buttonLayout === "stacked",
        })}
      >
        <Button variant="destructive" onClick={() => rejectMutation.mutate()}>
          <div>{humanInteractionBlock.negative_descriptor}</div>
        </Button>
        <Button variant="default" onClick={() => approveMutation.mutate()}>
          <div>{humanInteractionBlock.positive_descriptor}</div>
        </Button>
      </div>
    </div>
  );
}
