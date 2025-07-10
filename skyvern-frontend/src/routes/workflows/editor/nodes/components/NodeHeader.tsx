import { AxiosError } from "axios";
import { ReloadIcon, PlayIcon, StopIcon } from "@radix-ui/react-icons";
import { useEffect } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { getClient } from "@/api/AxiosClient";
import { ProxyLocation, User } from "@/api/types";
import { Timer } from "@/components/Timer";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useNodeLabelChangeHandler } from "@/routes/workflows/hooks/useLabelChangeHandler";
import { useDeleteNodeCallback } from "@/routes/workflows/hooks/useDeleteNodeCallback";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import {
  debuggableWorkflowBlockTypes,
  type WorkflowBlockType,
  type WorkflowApiResponse,
} from "@/routes/workflows/types/workflowTypes";
import { getInitialValues } from "@/routes/workflows/utils";
import { useDebugStore } from "@/store/useDebugStore";
import {
  useWorkflowSettingsStore,
  type WorkflowSettingsState,
} from "@/store/WorkflowSettingsStore";
import { cn } from "@/util/utils";
import {
  statusIsAFailureType,
  statusIsFinalized,
  statusIsRunningOrQueued,
} from "@/routes/tasks/types";
import {
  useOptimisticallyRequestBrowserSessionId,
  type OptimisticBrowserSession,
} from "@/store/useOptimisticallyRequestBrowserSessionId";
import { useUser } from "@/hooks/useUser";

import { EditableNodeTitle } from "../components/EditableNodeTitle";
import { NodeActionMenu } from "../NodeActionMenu";
import { WorkflowBlockIcon } from "../WorkflowBlockIcon";

interface Props {
  blockLabel: string; // today, this + wpid act as the identity of a block
  disabled?: boolean;
  editable: boolean;
  nodeId: string;
  totpIdentifier: string | null;
  totpUrl: string | null;
  type: WorkflowBlockType;
}

type Payload = Record<string, unknown> & {
  block_labels: string[];
  browser_session_id: string | null;
  extra_http_headers: Record<string, string> | null;
  max_screenshot_scrolls: number | null;
  parameters: Record<string, unknown>;
  proxy_location: ProxyLocation;
  totp_identifier: string | null;
  totp_url: string | null;
  webhook_url: string | null;
  workflow_id: string;
};

const blockTypeToTitle = (type: WorkflowBlockType): string => {
  const parts = type.split("_");
  const capCased = parts
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");

  return `${capCased} Block`;
};

const getPayload = (opts: {
  blockLabel: string;
  optimistic: OptimisticBrowserSession;
  parameters: Record<string, unknown>;
  totpIdentifier: string | null;
  totpUrl: string | null;
  user: User | null;
  workflowPermanentId: string;
  workflowSettings: WorkflowSettingsState;
}): Payload | null => {
  if (!opts.user) {
    toast({
      variant: "warning",
      title: "Error",
      description: "No user found",
    });

    return null;
  }

  const webhook_url = opts.workflowSettings.webhookCallbackUrl.trim();

  let extraHttpHeaders = null;

  try {
    extraHttpHeaders =
      opts.workflowSettings.extraHttpHeaders === null
        ? null
        : JSON.parse(opts.workflowSettings.extraHttpHeaders);
  } catch (e: unknown) {
    toast({
      variant: "warning",
      title: "Extra HTTP Headers",
      description: "Invalid extra HTTP Headers JSON",
    });
  }

  const browserSessionData = opts.optimistic.get(
    opts.user,
    opts.workflowPermanentId,
  );

  const browserSessionId = browserSessionData?.browser_session_id;

  if (!browserSessionId) {
    toast({
      variant: "warning",
      title: "Error",
      description: "No browser session ID found",
    });

    return null;
  } else {
    toast({
      variant: "default",
      title: "Success",
      description: `Browser session ID found: ${browserSessionId}`,
    });
  }

  const payload: Payload = {
    block_labels: [opts.blockLabel],
    browser_session_id: browserSessionId,
    extra_http_headers: extraHttpHeaders,
    max_screenshot_scrolls: opts.workflowSettings.maxScreenshotScrollingTimes,
    parameters: opts.parameters,
    proxy_location: opts.workflowSettings.proxyLocation,
    totp_identifier: opts.totpIdentifier,
    totp_url: opts.totpUrl,
    webhook_url: webhook_url.length > 0 ? webhook_url : null,
    workflow_id: opts.workflowPermanentId,
  };

  return payload;
};

function NodeHeader({
  blockLabel,
  disabled = false,
  editable,
  nodeId,
  totpIdentifier,
  totpUrl,
  type,
}: Props) {
  const {
    blockLabel: urlBlockLabel,
    workflowPermanentId,
    workflowRunId,
  } = useParams();
  const debugStore = useDebugStore();
  const thisBlockIsPlaying =
    urlBlockLabel !== undefined && urlBlockLabel === blockLabel;
  const anyBlockIsPlaying =
    urlBlockLabel !== undefined && urlBlockLabel.length > 0;
  const workflowSettingsStore = useWorkflowSettingsStore();
  const [label, setLabel] = useNodeLabelChangeHandler({
    id: nodeId,
    initialValue: blockLabel,
  });
  const blockTitle = blockTypeToTitle(type);
  const deleteNodeCallback = useDeleteNodeCallback();
  const credentialGetter = useCredentialGetter();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const location = useLocation();
  const isDebuggable = debuggableWorkflowBlockTypes.has(type);
  const { data: workflowRun } = useWorkflowRunQuery();
  const workflowRunIsRunningOrQueued =
    workflowRun && statusIsRunningOrQueued(workflowRun);
  const optimistic = useOptimisticallyRequestBrowserSessionId();
  const user = useUser().get();

  useEffect(() => {
    if (!workflowRun || !workflowPermanentId || !workflowRunId) {
      return;
    }

    if (
      workflowRunId === workflowRun?.workflow_run_id &&
      statusIsFinalized(workflowRun)
    ) {
      navigate(`/workflows/${workflowPermanentId}/debug`);

      if (statusIsAFailureType(workflowRun)) {
        toast({
          variant: "destructive",
          title: `Workflow Block ${urlBlockLabel}: ${workflowRun.status}`,
          description: `Reason: ${workflowRun.failure_reason}`,
        });
      } else if (statusIsFinalized(workflowRun)) {
        toast({
          variant: "success",
          title: `Workflow Block ${urlBlockLabel}: ${workflowRun.status}`,
        });
      }
    }
  }, [
    urlBlockLabel,
    navigate,
    workflowPermanentId,
    workflowRun,
    workflowRunId,
  ]);

  const runBlock = useMutation({
    mutationFn: async () => {
      if (!workflowPermanentId) {
        console.error("There is no workflowPermanentId");
        return;
      }

      const workflow = await queryClient.fetchQuery<WorkflowApiResponse>({
        queryKey: ["block", "workflow", workflowPermanentId],
        queryFn: async () => {
          const client = await getClient(credentialGetter);
          return client
            .get(`/workflows/${workflowPermanentId}`)
            .then((response) => response.data);
        },
      });

      const workflowParameters =
        workflow?.workflow_definition.parameters.filter(
          (parameter) => parameter.parameter_type === "workflow",
        );

      const parameters = getInitialValues(location, workflowParameters ?? []);

      const client = await getClient(credentialGetter, "sans-api-v1");

      const body = getPayload({
        blockLabel,
        optimistic,
        parameters,
        totpIdentifier,
        totpUrl,
        user,
        workflowPermanentId,
        workflowSettings: workflowSettingsStore,
      });

      if (!body) {
        return;
      }

      return await client.post<Payload, { data: { run_id: string } }>(
        "/run/workflows/blocks",
        body,
      );
    },
    onSuccess: (response) => {
      if (!response) {
        console.error("No response");
        return;
      }

      toast({
        variant: "success",
        title: "Workflow block run started",
        description: "The workflow block run has been started successfully",
      });

      navigate(
        `/workflows/${workflowPermanentId}/${response.data.run_id}/${label}/debug`,
      );
    },
    onError: (error: AxiosError) => {
      const detail = (error.response?.data as { detail?: string })?.detail;
      toast({
        variant: "destructive",
        title: "Failed to start workflow block run",
        description: detail ?? error.message,
      });
    },
  });

  const cancelBlock = useMutation({
    mutationFn: async () => {
      const browserSessionId =
        user && workflowPermanentId
          ? optimistic.get(user, workflowPermanentId)?.browser_session_id ??
            "<missing-browser-session-id>"
          : "<missing-user-or-workflow-permanent-id>";
      const client = await getClient(credentialGetter);
      return client
        .post(`/runs/${browserSessionId}/workflow_run/${workflowRunId}/cancel/`)
        .then((response) => response.data);
    },
    onSuccess: () => {
      toast({
        variant: "success",
        title: "Workflow Canceled",
        description: "The workflow has been successfully canceled.",
      });
      navigate(`/workflows/${workflowPermanentId}/debug`);
    },
    onError: (error) => {
      toast({
        variant: "destructive",
        title: "Error",
        description: error.message,
      });
    },
  });

  const handleOnPlay = () => {
    runBlock.mutate();
  };

  const handleOnCancel = () => {
    cancelBlock.mutate();
  };

  return (
    <>
      {thisBlockIsPlaying && (
        <div className="flex w-full animate-[auto-height_1s_ease-in-out_forwards] items-center justify-between overflow-hidden">
          <div className="pb-4">
            <Timer />
          </div>
          <div className="pb-4">{workflowRun?.status ?? "pending"}</div>
        </div>
      )}

      <header className="!mt-0 flex h-[2.75rem] justify-between gap-2">
        <div
          className={cn("flex gap-2", {
            "opacity-50": thisBlockIsPlaying,
          })}
        >
          <div className="flex h-[2.75rem] w-[2.75rem] items-center justify-center rounded border border-slate-600">
            <WorkflowBlockIcon workflowBlockType={type} className="size-6" />
          </div>
          <div className="flex flex-col gap-1">
            <EditableNodeTitle
              value={blockLabel}
              editable={editable}
              onChange={setLabel}
              titleClassName="text-base"
              inputClassName="text-base"
            />
            <span className="text-xs text-slate-400">{blockTitle}</span>
          </div>
        </div>
        <div className="pointer-events-auto ml-auto flex items-center gap-2">
          {thisBlockIsPlaying && workflowRunIsRunningOrQueued && (
            <div className="ml-auto">
              <button className="rounded p-1 hover:bg-red-500 hover:text-black disabled:opacity-50">
                {cancelBlock.isPending ? (
                  <ReloadIcon className="size-6 animate-spin" />
                ) : (
                  <StopIcon
                    className="size-6"
                    onClick={() => {
                      handleOnCancel();
                    }}
                  />
                )}
              </button>
            </div>
          )}
          {debugStore.isDebugMode && isDebuggable && (
            <button
              disabled={anyBlockIsPlaying}
              className={cn("rounded p-1 disabled:opacity-50", {
                "hover:bg-muted": anyBlockIsPlaying,
              })}
            >
              {runBlock.isPending ? (
                <ReloadIcon className="size-6 animate-spin" />
              ) : (
                <PlayIcon
                  className={cn("size-6", {
                    "fill-gray-500 text-gray-500":
                      anyBlockIsPlaying || !workflowPermanentId,
                  })}
                  onClick={() => {
                    handleOnPlay();
                  }}
                />
              )}
            </button>
          )}
          {disabled || debugStore.isDebugMode ? null : (
            <div>
              <div
                className={cn("rounded p-1 hover:bg-muted", {
                  "pointer-events-none opacity-50": anyBlockIsPlaying,
                })}
              >
                <NodeActionMenu
                  onDelete={() => {
                    deleteNodeCallback(nodeId);
                  }}
                />
              </div>
            </div>
          )}
        </div>
      </header>
    </>
  );
}

export { NodeHeader };
