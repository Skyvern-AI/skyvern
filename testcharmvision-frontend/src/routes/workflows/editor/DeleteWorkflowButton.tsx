import { getClient } from "@/api/AxiosClient";
import { GarbageIcon } from "@/components/icons/GarbageIcon";
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
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { ReloadIcon } from "@radix-ui/react-icons";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AxiosError } from "axios";

type Props = {
  id: string;
};

function DeleteWorkflowButton({ id }: Props) {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  const deleteWorkflowMutation = useMutation({
    mutationFn: async (id: string) => {
      const client = await getClient(credentialGetter);
      return client.delete(`/workflows/${id}`);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["workflows"],
      });
    },
    onError: (error: AxiosError) => {
      toast({
        variant: "destructive",
        title: "Failed to delete workflow",
        description: error.message,
      });
    },
  });

  return (
    <Dialog>
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <DialogTrigger asChild>
              <Button size="icon" variant="outline">
                <GarbageIcon className="h-4 w-4" />
              </Button>
            </DialogTrigger>
          </TooltipTrigger>
          <TooltipContent>Delete Workflow</TooltipContent>
        </Tooltip>
      </TooltipProvider>
      <DialogContent onCloseAutoFocus={(e) => e.preventDefault()}>
        <DialogHeader>
          <DialogTitle>Are you sure?</DialogTitle>
          <DialogDescription>This workflow will be deleted.</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="secondary">Cancel</Button>
          </DialogClose>
          <Button
            variant="destructive"
            onClick={() => {
              deleteWorkflowMutation.mutate(id);
            }}
            disabled={deleteWorkflowMutation.isPending}
          >
            {deleteWorkflowMutation.isPending && (
              <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />
            )}
            Delete
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export { DeleteWorkflowButton };
