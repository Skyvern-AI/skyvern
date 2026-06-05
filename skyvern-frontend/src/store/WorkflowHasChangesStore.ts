import { AxiosError } from "axios";
import { useEffect } from "react";
import { create } from "zustand";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { stringify as convertToYAML } from "yaml";
import { usePostHog } from "posthog-js/react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";

import { getClient } from "@/api/AxiosClient";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { isDraftWorkflowPermanentId } from "@/routes/workflows/draftWorkflow";
import {
  type BlockYAML,
  type ParameterYAML,
  WorkflowCreateYAMLRequest,
} from "@/routes/workflows/types/workflowYamlTypes";
import type {
  WorkflowApiResponse,
  WorkflowSettings,
} from "@/routes/workflows/types/workflowTypes";
type SaveData = {
  parameters: Array<ParameterYAML>;
  blocks: Array<BlockYAML>;
  workflowDefinitionVersion: number;
  title: string;
  settings: WorkflowSettings;
  workflow: WorkflowApiResponse;
};

type WorkflowHasChangesStore = {
  getSaveData: () => SaveData | null;
  hasChanges: boolean;
  saveIsPending: boolean;
  saidOkToCodeCacheDeletion: boolean;
  showConfirmCodeCacheDeletion: boolean;
  // Reference-counted flag: multiple concurrent internal updates won't
  // accidentally clear each other. Gate on > 0 in consumers.
  internalUpdateCount: number;
  setGetSaveData: (getSaveData: () => SaveData) => void;
  setHasChanges: (hasChanges: boolean) => void;
  setSaveIsPending: (isPending: boolean) => void;
  setSaidOkToCodeCacheDeletion: (saidOkToCodeCacheDeletion: boolean) => void;
  setShowConfirmCodeCacheDeletion: (show: boolean) => void;
  beginInternalUpdate: () => void;
  endInternalUpdate: () => void;
};

interface WorkflowSaveOpts {
  status?: string;
}

const useWorkflowHasChangesStore = create<WorkflowHasChangesStore>((set) => {
  return {
    hasChanges: false,
    saveIsPending: false,
    saidOkToCodeCacheDeletion: false,
    showConfirmCodeCacheDeletion: false,
    internalUpdateCount: 0,
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
    setSaidOkToCodeCacheDeletion: (saidOkToCodeCacheDeletion: boolean) => {
      set({ saidOkToCodeCacheDeletion });
    },
    setShowConfirmCodeCacheDeletion: (show: boolean) => {
      set({ showConfirmCodeCacheDeletion: show });
    },
    beginInternalUpdate: () => {
      set((state) => ({ internalUpdateCount: state.internalUpdateCount + 1 }));
    },
    endInternalUpdate: () => {
      set((state) => ({
        internalUpdateCount: Math.max(0, state.internalUpdateCount - 1),
      }));
    },
  };
});

const useWorkflowSave = (opts?: WorkflowSaveOpts) => {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const postHog = usePostHog();
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams] = useSearchParams();
  const {
    getSaveData,
    saidOkToCodeCacheDeletion,
    setHasChanges,
    setSaveIsPending,
    setShowConfirmCodeCacheDeletion,
  } = useWorkflowHasChangesStore();

  const saveWorkflowMutation = useMutation({
    mutationFn: async () => {
      const saveData = getSaveData();

      if (!saveData) {
        setHasChanges(false);
        return null;
      }

      const isDraft = isDraftWorkflowPermanentId(
        saveData.workflow.workflow_permanent_id,
      );

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

      let cdpConnectHeaders: Record<string, string> | null = null;
      if (saveData.settings.cdpConnectHeaders) {
        try {
          const parsedCdpHeaders = JSON.parse(
            saveData.settings.cdpConnectHeaders,
          );
          if (
            parsedCdpHeaders &&
            typeof parsedCdpHeaders === "object" &&
            !Array.isArray(parsedCdpHeaders)
          ) {
            // Send the dict as-is, including any mask sentinels for unedited
            // entries. The backend resolves entries key-by-key so a newly added
            // key alongside a masked one is preserved (not wiped).
            const sanitized: Record<string, string> = {};
            for (const [key, value] of Object.entries(parsedCdpHeaders)) {
              if (key && typeof key === "string") {
                sanitized[key] = String(value);
              }
            }
            cdpConnectHeaders = sanitized;
          }
        } catch (error) {
          toast({
            title: "Error",
            description: "Invalid JSON format in cdp connect headers",
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
        browser_profile_id: saveData.settings.browserProfileId,
        model: saveData.settings.model,
        max_screenshot_scrolls: saveData.settings.maxScreenshotScrolls,
        max_elapsed_time_minutes:
          saveData.settings.maxElapsedTimeMinutes ?? null,
        totp_verification_url: saveData.workflow.totp_verification_url,
        extra_http_headers: extraHttpHeaders,
        cdp_connect_headers: cdpConnectHeaders,
        run_with: saveData.settings.runWith,
        cache_key: normalizedKey,
        ai_fallback: saveData.settings.aiFallback ?? true,
        code_version:
          saveData.settings.runWith === "code"
            ? (saveData.settings.codeVersion ?? 2)
            : undefined,
        workflow_definition: {
          version: saveData.workflowDefinitionVersion,
          parameters: saveData.parameters,
          blocks: saveData.blocks,
          finally_block_label: saveData.settings.finallyBlockLabel ?? undefined,
          workflow_system_prompt:
            saveData.settings.workflowSystemPrompt ?? undefined,
        },
        is_saved_task: saveData.workflow.is_saved_task,
        status: opts?.status ?? saveData.workflow.status,
        run_sequentially: saveData.settings.runSequentially,
        sequential_key: saveData.settings.sequentialKey,
        ...(isDraft && saveData.workflow.folder_id
          ? { folder_id: saveData.workflow.folder_id }
          : {}),
      };

      const yaml = convertToYAML(requestBody);

      if (isDraft) {
        const created = await client.post<
          string,
          { data: WorkflowApiResponse }
        >("/workflows", yaml, {
          headers: {
            "Content-Type": "text/plain",
          },
        });
        return {
          saveData,
          createdWorkflow: created.data,
          isDraft: true as const,
        };
      }

      const updated = await client.put<string, WorkflowApiResponse>(
        `/workflows/${saveData.workflow.workflow_permanent_id}`,
        yaml,
        {
          headers: {
            "Content-Type": "text/plain",
          },
          params: {
            delete_code_cache_is_ok: saidOkToCodeCacheDeletion
              ? "true"
              : "false",
          },
        },
      );
      return { saveData, createdWorkflow: updated, isDraft: false as const };
    },
    onSuccess: (result) => {
      if (!result) {
        return;
      }

      const { saveData, createdWorkflow, isDraft } = result;
      const workflowPermanentId = createdWorkflow.workflow_permanent_id;

      postHog.capture("builder.workflow.saved", {
        org_id:
          createdWorkflow.organization_id || saveData.workflow.organization_id,
        workflow_permanent_id: workflowPermanentId,
        block_count: saveData.blocks.length,
        block_types: saveData.blocks.map((b) => b.block_type),
      });

      toast({
        title: isDraft ? "Agent created" : "Changes saved",
        description: isDraft
          ? "Your agent has been saved"
          : "Your changes have been saved",
        variant: "success",
      });

      if (isDraft) {
        const via = searchParams.get("via");
        const nextSearch = via ? `?via=${encodeURIComponent(via)}` : "";
        navigate(`/workflows/${workflowPermanentId}/build${nextSearch}`, {
          replace: true,
          state: location.state,
        });
        queryClient.setQueryData(
          ["workflow", workflowPermanentId],
          createdWorkflow,
        );
      } else {
        queryClient.invalidateQueries({
          queryKey: ["workflow", workflowPermanentId],
        });
      }

      queryClient.invalidateQueries({
        queryKey: ["workflows"],
      });
      queryClient.invalidateQueries({
        queryKey: ["folders"],
      });

      queryClient.invalidateQueries({
        queryKey: ["block-scripts", workflowPermanentId],
      });

      setHasChanges(false);
    },
    onError: (error: AxiosError) => {
      const responseData = error.response?.data as
        | {
            detail?:
              | string
              | Array<{
                  loc?: Array<string | number>;
                  msg?: string;
                  type?: string;
                }>;
          }
        | undefined;
      const rawDetail = responseData?.detail;

      if (
        typeof rawDetail === "string" &&
        rawDetail.startsWith("No confirmation for code cache deletion")
      ) {
        setShowConfirmCodeCacheDeletion(true);
        return;
      }

      let description: string;
      if (typeof rawDetail === "string" && rawDetail) {
        description = rawDetail;
      } else if (Array.isArray(rawDetail) && rawDetail.length > 0) {
        // FastAPI's own 422 responses (e.g. request body validation) return detail
        // as an array; our custom ValidationError handler returns it as a string.
        description = rawDetail
          .map((err) => {
            const loc = err.loc
              ?.filter((part) => part !== "body" && part !== "__root__")
              .join(" -> ");
            return loc ? `${loc}: ${err.msg}` : (err.msg ?? "Unknown error");
          })
          .join("; ");
      } else {
        description =
          "Failed to save agent. Please check your agent configuration and try again.";
      }

      toast({
        title: "Failed to save agent",
        description,
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
