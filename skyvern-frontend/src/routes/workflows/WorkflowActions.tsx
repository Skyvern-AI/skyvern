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
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import {
  CopyIcon,
  DotsHorizontalIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AxiosError } from "axios";
import { useWorkflowQuery } from "./hooks/useWorkflowQuery";
import { stringify as convertToYAML } from "yaml";
import { WorkflowApiResponse } from "./types/workflowTypes";
import { useNavigate } from "react-router-dom";
import { WorkflowCreateYAMLRequest } from "./types/workflowYamlTypes";
import { convert } from "./editor/workflowEditorUtils";
import { GarbageIcon } from "@/components/icons/GarbageIcon";

type Props = {
  id: string;
};

function WorkflowActions({ id }: Props) {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const { data: workflow } = useWorkflowQuery({ workflowPermanentId: id });
  const navigate = useNavigate();

  const createWorkflowMutation = useMutation({
    mutationFn: async (workflow: WorkflowCreateYAMLRequest) => {
      const client = await getClient(credentialGetter);
      const yaml = convertToYAML(workflow);
      return client.post<string, { data: WorkflowApiResponse }>(
        "/workflows",
        yaml,
        {
          headers: {
            "Content-Type": "text/plain",
          },
        },
      );
    },
    onSuccess: (response) => {
      queryClient.invalidateQueries({
        queryKey: ["workflows"],
      });
      navigate(`/workflows/${response.data.workflow_permanent_id}/edit`);
    },
  });

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
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button size="icon" variant="outline">
            <DotsHorizontalIcon className="h-4 w-4" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent>
          <DropdownMenuItem
            onSelect={() => {
              if (!workflow) {
                return;
              }
              const clonedWorkflow = convert(workflow);
              createWorkflowMutation.mutate(clonedWorkflow);
            }}
            className="p-2"
          >
            <CopyIcon className="mr-2 h-4 w-4" />
            Clone Workflow
          </DropdownMenuItem>
          <DialogTrigger>
            <DropdownMenuItem className="p-2">
              <GarbageIcon className="mr-2 h-4 w-4 text-destructive" />
              Delete Workflow
            </DropdownMenuItem>
          </DialogTrigger>
        </DropdownMenuContent>
      </DropdownMenu>
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

export { WorkflowActions };
