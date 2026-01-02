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
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuPortal,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import {
  BookmarkFilledIcon,
  BookmarkIcon,
  CopyIcon,
  DotsHorizontalIcon,
  DownloadIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AxiosError } from "axios";
import { stringify as convertToYAML } from "yaml";
import { convert } from "./editor/workflowEditorUtils";
import { useCreateWorkflowMutation } from "./hooks/useCreateWorkflowMutation";
import { WorkflowApiResponse } from "./types/workflowTypes";

type Props = {
  workflow: WorkflowApiResponse;
  onSuccessfullyDeleted?: () => void;
};

function downloadFile(fileName: string, contents: string) {
  const element = document.createElement("a");
  element.setAttribute(
    "href",
    "data:text/plain;charset=utf-8," + encodeURIComponent(contents),
  );
  element.setAttribute("download", fileName);

  element.style.display = "none";
  document.body.appendChild(element);

  element.click();

  document.body.removeChild(element);
}

function WorkflowActions({ workflow, onSuccessfullyDeleted }: Props) {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  function handleExport(type: "json" | "yaml") {
    if (!workflow) {
      return;
    }
    const fileName = `${workflow.title}.${type}`;
    const contents =
      type === "json"
        ? JSON.stringify(convert(workflow), null, 2)
        : convertToYAML(convert(workflow));
    downloadFile(fileName, contents);
  }

  const createWorkflowMutation = useCreateWorkflowMutation();

  const deleteWorkflowMutation = useMutation({
    mutationFn: async (id: string) => {
      const client = await getClient(credentialGetter);
      return client.delete(`/workflows/${id}`);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["workflows"],
      });
      queryClient.invalidateQueries({
        queryKey: ["folders"],
      });
      onSuccessfullyDeleted?.();
    },
    onError: (error: AxiosError) => {
      toast({
        variant: "destructive",
        title: "Failed to delete workflow",
        description: error.message,
      });
    },
  });

  const templateMutation = useMutation({
    mutationFn: async ({
      workflowPermanentId,
      isTemplate,
    }: {
      workflowPermanentId: string;
      isTemplate: boolean;
    }) => {
      // Template endpoint only exists on /v1 (no /api prefix)
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client.put(
        `/workflows/${workflowPermanentId}/template?is_template=${isTemplate}`,
      );
    },
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({
        queryKey: ["workflows"],
      });
      queryClient.invalidateQueries({
        queryKey: ["orgTemplates"],
      });
      queryClient.invalidateQueries({
        queryKey: ["workflow", variables.workflowPermanentId],
      });
      toast({
        title: variables.isTemplate
          ? "Saved as template"
          : "Removed from templates",
        variant: "success",
      });
    },
    onError: (error: AxiosError) => {
      toast({
        variant: "destructive",
        title: "Failed to update template status",
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
              const clonedWorkflow = convert({
                ...workflow,
                title: `Copy of ${workflow.title}`,
              });
              createWorkflowMutation.mutate(clonedWorkflow);
            }}
            className="p-2"
          >
            <CopyIcon className="mr-2 h-4 w-4" />
            Clone Workflow
          </DropdownMenuItem>
          <DropdownMenuItem
            onSelect={() => {
              templateMutation.mutate({
                workflowPermanentId: workflow.workflow_permanent_id,
                isTemplate: !workflow.is_template,
              });
            }}
            className="p-2"
            disabled={templateMutation.isPending}
          >
            {workflow.is_template ? (
              <BookmarkFilledIcon className="mr-2 h-4 w-4" />
            ) : (
              <BookmarkIcon className="mr-2 h-4 w-4" />
            )}
            {workflow.is_template
              ? "Remove from Templates"
              : "Save as Template"}
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuSub>
            <DropdownMenuSubTrigger>
              <DownloadIcon className="mr-2 h-4 w-4" />
              Export as...
            </DropdownMenuSubTrigger>
            <DropdownMenuPortal>
              <DropdownMenuSubContent>
                <DropdownMenuItem
                  onSelect={() => {
                    handleExport("yaml");
                  }}
                >
                  YAML
                </DropdownMenuItem>
                <DropdownMenuItem
                  onSelect={() => {
                    handleExport("json");
                  }}
                >
                  JSON
                </DropdownMenuItem>
              </DropdownMenuSubContent>
            </DropdownMenuPortal>
          </DropdownMenuSub>
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
          <DialogDescription>
            The workflow{" "}
            <span className="font-semibold text-primary">{workflow.title}</span>{" "}
            will be deleted.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="secondary">Cancel</Button>
          </DialogClose>
          <Button
            variant="destructive"
            onClick={() => {
              deleteWorkflowMutation.mutate(workflow.workflow_permanent_id);
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
