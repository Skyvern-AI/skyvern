import {
  CounterClockwiseClockIcon,
  DotsHorizontalIcon,
  ResetIcon,
} from "@radix-ui/react-icons";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type AxiosError } from "axios";
import { useParams } from "react-router-dom";

import { getClient } from "@/api/AxiosClient";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuShortcut,
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
import { statusIsRunningOrQueued } from "@/routes/tasks/types";
import { useWorkflowQuery } from "@/routes/workflows/hooks/useWorkflowQuery";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import { useRecordingStore } from "@/store/useRecordingStore";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import { useWorkflowHistoryAccessStore } from "@/store/WorkflowHistoryAccessStore";

import { useUndoRedoShortcutLabels } from "../controls/useUndoRedoShortcutLabels";
import { useSaveWorkflow } from "../hooks/useSaveWorkflow";
import { useToggleHistoryPanel } from "../hooks/useToggleHistoryPanel";
import { CodeSubmenu } from "./CodeSubmenu";

export function EditorOverflowMenu() {
  const { workflowPermanentId } = useParams();
  const { data: workflow } = useWorkflowQuery({ workflowPermanentId });
  const { data: workflowRun } = useWorkflowRunQuery();
  const isTemplate = workflow?.is_template ?? false;
  const saving = useWorkflowHasChangesStore((s) => s.saveIsPending);
  const isRecording = useRecordingStore((s) => s.isRecording);
  const onSave = useSaveWorkflow();
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  const canUndo = useWorkflowHistoryAccessStore((s) => s.canUndo);
  const onUndo = useWorkflowHistoryAccessStore((s) => s.undo);
  const canRedo = useWorkflowHistoryAccessStore((s) => s.canRedo);
  const onRedo = useWorkflowHistoryAccessStore((s) => s.redo);
  const { undoShortcutLabel, redoShortcutLabel } = useUndoRedoShortcutLabels();

  const toggleHistoryPanel = useToggleHistoryPanel();

  const workflowRunIsRunningOrQueued = Boolean(
    workflowRun && statusIsRunningOrQueued(workflowRun),
  );

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
                disabled={isRecording}
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
      <DropdownMenuContent align="end" className="min-w-[12rem]">
        <CodeSubmenu />
        <DropdownMenuItem
          disabled={!canUndo || isRecording}
          onSelect={(event) => {
            event.preventDefault();
            onUndo();
          }}
        >
          <ResetIcon className="mr-2 size-4" />
          Undo
          <DropdownMenuShortcut>{undoShortcutLabel}</DropdownMenuShortcut>
        </DropdownMenuItem>
        <DropdownMenuItem
          disabled={!canRedo || isRecording}
          onSelect={(event) => {
            event.preventDefault();
            onRedo();
          }}
        >
          <ResetIcon className="mr-2 size-4 -scale-x-100" />
          Redo
          <DropdownMenuShortcut>{redoShortcutLabel}</DropdownMenuShortcut>
        </DropdownMenuItem>
        {!workflowRunIsRunningOrQueued && (
          <DropdownMenuItem
            disabled={isRecording}
            onSelect={() => toggleHistoryPanel()}
          >
            <CounterClockwiseClockIcon className="mr-2 size-4" />
            Version history
          </DropdownMenuItem>
        )}
        <DropdownMenuSeparator />
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
