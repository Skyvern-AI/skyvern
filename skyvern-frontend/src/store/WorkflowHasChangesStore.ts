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
  setGetSaveData: (getSaveData: () => SaveData) => void;
  setHasChanges: (hasChanges: boolean) => void;
  setSaveIsPending: (isPending: boolean) => void;
};

const useWorkflowHasChangesStore = create<WorkflowHasChangesStore>((set) => {
  return {
    hasChanges: false,
    saveIsPending: false,
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
  };
});

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
  type SaveData as WorkflowSaveData,
};
