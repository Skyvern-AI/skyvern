import { getClient } from "@/api/AxiosClient";
import { GarbageIcon } from "@/components/icons/GarbageIcon";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AxiosError } from "axios";
import { useState } from "react";

import { useNodeCollapseStore } from "./collapse/useNodeCollapseStore";

type Props = {
  id: string;
};

function DeleteWorkflowButton({ id }: Props) {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);

  const deleteWorkflowMutation = useMutation({
    mutationFn: async (id: string) => {
      const client = await getClient(credentialGetter);
      return client.delete(`/workflows/${id}`);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["workflows"],
      });
      useNodeCollapseStore.getState().pruneWorkflow(id);
    },
    onError: (error: AxiosError) => {
      toast({
        variant: "destructive",
        title: "Failed to delete agent",
        description: error.message,
      });
    },
  });

  return (
    <>
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button size="icon" variant="outline" onClick={() => setOpen(true)}>
              <GarbageIcon className="h-4 w-4" />
            </Button>
          </TooltipTrigger>
          <TooltipContent>Delete Agent</TooltipContent>
        </Tooltip>
      </TooltipProvider>
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
        title="Delete agent?"
        description={<p>This agent will be permanently deleted.</p>}
        isPending={deleteWorkflowMutation.isPending}
        onConfirm={() => {
          deleteWorkflowMutation.mutate(id);
        }}
      />
    </>
  );
}

export { DeleteWorkflowButton };
