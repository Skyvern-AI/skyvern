import { getClient } from "@/api/AxiosClient";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AxiosError } from "axios";
import { stringify as convertToYAML } from "yaml";
import { useNodeCollapseStore } from "../editor/collapse/useNodeCollapseStore";
import { convert } from "../editor/workflowEditorUtils";
import { useCreateWorkflowMutation } from "./useCreateWorkflowMutation";
import { WorkflowApiResponse } from "../types/workflowTypes";

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

// Single-row mutations shared by WorkflowActions (the workflow page's actions
// dropdown) and the agents-list right-click context menu so they can't drift.
function useWorkflowRowActions(workflow: WorkflowApiResponse) {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const createWorkflowMutation = useCreateWorkflowMutation();

  const deleteWorkflowMutation = useMutation({
    mutationFn: async (id: string) => {
      const client = await getClient(credentialGetter);
      return client.delete(`/workflows/${id}`);
    },
    onSuccess: (_data, deletedId) => {
      queryClient.invalidateQueries({
        queryKey: ["workflows"],
      });
      queryClient.invalidateQueries({
        queryKey: ["folders"],
      });
      useNodeCollapseStore.getState().pruneWorkflow(deletedId);
    },
    onError: (error: AxiosError) => {
      toast({
        variant: "destructive",
        title: "Failed to delete agent",
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

  function clone() {
    const clonedWorkflow = convert({
      ...workflow,
      title: `Copy of ${workflow.title}`,
    });
    createWorkflowMutation.mutate(clonedWorkflow);
  }

  function toggleTemplate() {
    templateMutation.mutate({
      workflowPermanentId: workflow.workflow_permanent_id,
      isTemplate: !workflow.is_template,
    });
  }

  function exportAs(type: "json" | "yaml") {
    const fileName = `${workflow.title}.${type}`;
    const contents =
      type === "json"
        ? JSON.stringify(convert(workflow), null, 2)
        : convertToYAML(convert(workflow));
    downloadFile(fileName, contents);
  }

  function deleteWorkflow(options?: { onSuccess?: () => void }) {
    deleteWorkflowMutation.mutate(workflow.workflow_permanent_id, {
      onSuccess: options?.onSuccess,
    });
  }

  return {
    clone,
    toggleTemplate,
    exportAs,
    deleteWorkflow,
    isDeleting: deleteWorkflowMutation.isPending,
    isTogglingTemplate: templateMutation.isPending,
  };
}

export { useWorkflowRowActions };
