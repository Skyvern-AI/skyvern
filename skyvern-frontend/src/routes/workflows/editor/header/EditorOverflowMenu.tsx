import { DotsHorizontalIcon } from "@radix-ui/react-icons";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type AxiosError } from "axios";
import { useParams } from "react-router-dom";

import { getClient } from "@/api/AxiosClient";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useWorkflowQuery } from "@/routes/workflows/hooks/useWorkflowQuery";
import { useRecordingStore } from "@/store/useRecordingStore";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";

import { useSaveWorkflow } from "../hooks/useSaveWorkflow";

export function EditorOverflowMenu() {
  const { workflowPermanentId } = useParams();
  const { data: workflow } = useWorkflowQuery({ workflowPermanentId });
  const isTemplate = workflow?.is_template ?? false;
  const saving = useWorkflowHasChangesStore((s) => s.saveIsPending);
  const isRecording = useRecordingStore((s) => s.isRecording);
  const onSave = useSaveWorkflow();
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  const templateMutation = useMutation({
    mutationFn: async (newIsTemplate: boolean) => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client.put(
        `/workflows/${workflowPermanentId}/template?is_template=${newIsTemplate}`,
      );
    },
    onSuccess: (_, newIsTemplate) => {
      queryClient.invalidateQueries({ queryKey: ["workflows"] });
      queryClient.invalidateQueries({ queryKey: ["orgTemplates"] });
      queryClient.invalidateQueries({
        queryKey: ["workflow", workflowPermanentId],
      });
      toast({
        title: newIsTemplate ? "Saved as template" : "Removed from templates",
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

  const disabled = isRecording || templateMutation.isPending || saving;

  const handleTemplateToggle = () => {
    const newIsTemplate = !isTemplate;
    if (newIsTemplate) {
      void onSave();
    }
    templateMutation.mutate(newIsTemplate);
  };

  return (
    <DropdownMenu modal={false}>
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <DropdownMenuTrigger asChild>
              <Button
                disabled={disabled}
                size="icon"
                variant="tertiary"
                className="size-10 min-w-[2.5rem]"
                aria-label="More actions"
              >
                <DotsHorizontalIcon className="size-5" />
              </Button>
            </DropdownMenuTrigger>
          </TooltipTrigger>
          <TooltipContent>More actions</TooltipContent>
        </Tooltip>
      </TooltipProvider>
      <DropdownMenuContent align="end">
        <DropdownMenuItem
          disabled={disabled}
          onSelect={(event) => {
            if (disabled) {
              event.preventDefault();
              return;
            }
            handleTemplateToggle();
          }}
        >
          {isTemplate ? "Remove from Templates" : "Save as Template"}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
