import { AxiosError } from "axios";
import { useEffect } from "react";
import { create } from "zustand";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { stringify as convertToYAML } from "yaml";
import { usePostHog } from "posthog-js/react";

import { getClient } from "@/api/AxiosClient";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { getJsonParseErrorDetail } from "@/util/jsonParseError";
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
  const {
    getSaveData,
    saidOkToCodeCacheDeletion,
    setHasChanges,
    setSaveIsPending,
    setSaidOkToCodeCacheDeletion,
    setShowConfirmCodeCacheDeletion,
  } = useWorkflowHasChangesStore();

  const saveWorkflowMutation = useMutation({
    mutationFn: async (override?: Partial<SaveData>) => {
      const base = getSaveData();

      if (!base) {
        setHasChanges(false);
        return;
      }
      // YAML-mode saves pass the parsed draft (blocks/parameters/version, plus
      // a corrected finally_block_label) so we persist the edit directly
      // instead of the graph, which lags a commit's async setNodes.
      const saveData: SaveData = override ? { ...base, ...override } : base;

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
            description: `Invalid JSON format in extra http headers: ${getJsonParseErrorDetail(
              saveData.settings.extraHttpHeaders ?? "",
              error,
            )}`,
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
            description: `Invalid JSON format in cdp connect headers: ${getJsonParseErrorDetail(
              saveData.settings.cdpConnectHeaders ?? "",
              error,
            )}`,
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
        browser_profile_key: saveData.settings.browserProfileKey,
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
        enable_self_healing: saveData.settings.enableSelfHealing ?? false,
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
          error_code_mapping: saveData.settings.errorCodeMapping ?? undefined,
        },
        is_saved_task: saveData.workflow.is_saved_task,
        status: opts?.status ?? saveData.workflow.status,
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
          params: {
            delete_code_cache_is_ok: saidOkToCodeCacheDeletion
              ? "true"
              : "false",
          },
        },
      );
    },
    onSuccess: () => {
      // The confirmation authorizes exactly one save. Resetting here covers
      // every persist path (top bar, nav blocker, YAML mode), so a later save
      // can't silently delete cached code without re-prompting.
      setSaidOkToCodeCacheDeletion(false);

      const saveData = getSaveData();

      if (!saveData) {
        return;
      }

      postHog.capture("builder.workflow.saved", {
        org_id: saveData.workflow.organization_id,
        workflow_permanent_id: saveData.workflow.workflow_permanent_id,
        block_count: saveData.blocks.length,
        block_types: saveData.blocks.map((b) => b.block_type),
      });

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
