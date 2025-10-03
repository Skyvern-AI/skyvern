import { AxiosError } from "axios";
import { useEffect } from "react";
import { create } from "zustand";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { stringify as convertToYAML } from "yaml";

import { getClient } from "@/api/AxiosClient";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import type {
  BlockYAML,
  ParameterYAML,
} from "@/routes/workflows/types/workflowYamlTypes";
import type {
  WorkflowApiResponse,
  WorkflowSettings,
} from "@/routes/workflows/types/workflowTypes";
import { WorkflowCreateYAMLRequest } from "@/routes/workflows/types/workflowYamlTypes";
type SaveData = {
  parameters: Array<ParameterYAML>;
  blocks: Array<BlockYAML>;
  title: string;
  settings: WorkflowSettings;
  workflow: WorkflowApiResponse;
};

type WorkflowHasChangesStore = {
  getSaveData: () => SaveData | null;
  hasChanges: boolean;
  saveIsPending: boolean;
  workflowDefinitionChanged: boolean;
  setGetSaveData: (getSaveData: () => SaveData) => void;
  setHasChanges: (hasChanges: boolean) => void;
  setSaveIsPending: (isPending: boolean) => void;
  setWorkflowDefinitionChanged: (changed: boolean) => void;
};

/**
 * Helper function to normalize workflow definition for comparison
 * Removes fields that shouldn't affect equality (similar to backend's _get_workflow_definition_without_dates)
 */
function normalizeWorkflowDefinition(definition: {
  parameters: Array<ParameterYAML>;
  blocks: Array<BlockYAML>;
}): string {
  const fieldsToRemove = [
    "created_at",
    "modified_at",
    "deleted_at",
    "output_parameter_id",
    "workflow_id",
    "workflow_parameter_id",
  ];

  const removeFields = (obj: any): any => {
    if (Array.isArray(obj)) {
      return obj.map(removeFields);
    } else if (obj !== null && typeof obj === "object") {
      const newObj: any = {};
      for (const [key, value] of Object.entries(obj)) {
        if (!fieldsToRemove.includes(key)) {
          newObj[key] = removeFields(value);
        }
      }
      return newObj;
    }
    return obj;
  };

  const normalized = removeFields(definition);
  return JSON.stringify(normalized);
}

const useWorkflowHasChangesStore = create<WorkflowHasChangesStore>((set) => {
  return {
    hasChanges: false,
    saveIsPending: false,
    workflowDefinitionChanged: false,
    getSaveData: () => null,
    setGetSaveData: (getSaveData: () => SaveData) => {
      set({ getSaveData });
    },
    setHasChanges: (hasChanges: boolean) => {
      set({ hasChanges });
    },
    setSaveIsPending: (isPending: boolean) => {
      set({ saveIsPending: isPending });
    },
    setWorkflowDefinitionChanged: (changed: boolean) => {
      set({ workflowDefinitionChanged: changed });
    },
  };
});

/**
 * Hook to check if workflow definition has changed compared to the original
 */
const useCheckWorkflowDefinitionChanged = () => {
  const { getSaveData, setWorkflowDefinitionChanged } =
    useWorkflowHasChangesStore();

  const checkDefinitionChanged = () => {
    const saveData = getSaveData();
    if (!saveData) {
      setWorkflowDefinitionChanged(false);
      return false;
    }

    const currentDefinition = normalizeWorkflowDefinition({
      parameters: saveData.parameters,
      blocks: saveData.blocks,
    });

    const originalDefinition = normalizeWorkflowDefinition({
      parameters: saveData.workflow.workflow_definition.parameters,
      blocks: saveData.workflow.workflow_definition.blocks,
    });

    const changed = currentDefinition !== originalDefinition;
    setWorkflowDefinitionChanged(changed);
    return changed;
  };

  return checkDefinitionChanged;
};

const useWorkflowSave = () => {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const { getSaveData, setHasChanges, setSaveIsPending } =
    useWorkflowHasChangesStore();

  const saveWorkflowMutation = useMutation({
    mutationFn: async () => {
      const saveData = getSaveData();

      if (!saveData) {
        setHasChanges(false);
        return;
      }

      const client = await getClient(credentialGetter);
      const extraHttpHeaders: Record<string, string> = {};

      if (saveData.settings.extraHttpHeaders) {
        try {
          const parsedHeaders = JSON.parse(saveData.settings.extraHttpHeaders);
          if (
            parsedHeaders &&
            typeof parsedHeaders === "object" &&
            !Array.isArray(parsedHeaders)
          ) {
            for (const [key, value] of Object.entries(parsedHeaders)) {
              if (key && typeof key === "string") {
                if (key in extraHttpHeaders) {
                  toast({
                    title: "Error",
                    description: `Duplicate key '${key}' in extra http headers`,
                    variant: "destructive",
                  });
                  continue;
                }
                extraHttpHeaders[key] = String(value);
              }
            }
          }
        } catch (error) {
          toast({
            title: "Error",
            description: "Invalid JSON format in extra http headers",
            variant: "destructive",
          });
          return;
        }
      }

      const scriptCacheKey = saveData.settings.scriptCacheKey ?? "";
      const normalizedKey =
        scriptCacheKey === "" ? "default" : saveData.settings.scriptCacheKey;

      const requestBody: WorkflowCreateYAMLRequest = {
        title: saveData.title,
        description: saveData.workflow.description,
        proxy_location: saveData.settings.proxyLocation,
        webhook_callback_url: saveData.settings.webhookCallbackUrl,
        persist_browser_session: saveData.settings.persistBrowserSession,
        model: saveData.settings.model,
        max_screenshot_scrolls: saveData.settings.maxScreenshotScrolls,
        totp_verification_url: saveData.workflow.totp_verification_url,
        extra_http_headers: extraHttpHeaders,
        run_with: saveData.settings.runWith,
        cache_key: normalizedKey,
        ai_fallback: saveData.settings.aiFallback ?? true,
        workflow_definition: {
          parameters: saveData.parameters,
          blocks: saveData.blocks,
        },
        is_saved_task: saveData.workflow.is_saved_task,
        run_sequentially: saveData.settings.runSequentially,
        sequential_key: saveData.settings.sequentialKey,
      };

      const yaml = convertToYAML(requestBody);

      return client.put<string, WorkflowApiResponse>(
        `/workflows/${saveData.workflow.workflow_permanent_id}`,
        yaml,
        {
          headers: {
            "Content-Type": "text/plain",
          },
        },
      );
    },
    onSuccess: () => {
      const saveData = getSaveData();

      if (!saveData) {
        return;
      }

      toast({
        title: "Changes saved",
        description: "Your changes have been saved",
        variant: "success",
      });

      queryClient.invalidateQueries({
        queryKey: ["workflow", saveData.workflow.workflow_permanent_id],
      });

      queryClient.invalidateQueries({
        queryKey: ["workflows"],
      });

      queryClient.invalidateQueries({
        queryKey: ["block-scripts", saveData.workflow.workflow_permanent_id],
      });

      setHasChanges(false);
    },
    onError: (error: AxiosError) => {
      const detail = (error.response?.data as { detail?: string })?.detail;

      toast({
        title: "Error",
        description: detail ? detail : error.message,
        variant: "destructive",
      });
    },
  });

  useEffect(() => {
    setSaveIsPending(saveWorkflowMutation.isPending);
  }, [saveWorkflowMutation.isPending, setSaveIsPending]);

  return saveWorkflowMutation;
};

export {
  useWorkflowSave,
  useWorkflowHasChangesStore,
  useCheckWorkflowDefinitionChanged,
  type SaveData as WorkflowSaveData,
};
