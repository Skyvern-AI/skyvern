import { AxiosError } from "axios";
import { ReloadIcon, PlayIcon, StopIcon } from "@radix-ui/react-icons";
import { useEffect, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { getClient } from "@/api/AxiosClient";
import { ProxyLocation, Status } from "@/api/types";
import { NoticeMe } from "@/components/NoticeMe";
import { StatusBadge } from "@/components/StatusBadge";
import { toast } from "@/components/ui/use-toast";
import { useLogging } from "@/hooks/useLogging";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useOnChange } from "@/hooks/useOnChange";
import { useAutoplayStore } from "@/store/useAutoplayStore";

import { useNodeLabelChangeHandler } from "@/routes/workflows/hooks/useLabelChangeHandler";
import { useDeleteNodeCallback } from "@/routes/workflows/hooks/useDeleteNodeCallback";
import { useTransmuteNodeCallback } from "@/routes/workflows/hooks/useTransmuteNodeCallback";
import { useToggleScriptForNodeCallback } from "@/routes/workflows/hooks/useToggleScriptForNodeCallback";
import { useDebugSessionQuery } from "@/routes/workflows/hooks/useDebugSessionQuery";
import { useWorkflowQuery } from "@/routes/workflows/hooks/useWorkflowQuery";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import {
  debuggableWorkflowBlockTypes,
  scriptableWorkflowBlockTypes,
  type WorkflowBlockType,
  type WorkflowApiResponse,
} from "@/routes/workflows/types/workflowTypes";
import { getInitialValues } from "@/routes/workflows/utils";
import { useBlockOutputStore } from "@/store/BlockOutputStore";
import { useDebugStore } from "@/store/useDebugStore";
import { useRecordingStore } from "@/store/useRecordingStore";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";
import { useWorkflowSave } from "@/store/WorkflowHasChangesStore";
import {
  useWorkflowSettingsStore,
  type WorkflowSettingsState,
} from "@/store/WorkflowSettingsStore";
import { cn, formatDate, toDate } from "@/util/utils";
import {
  statusIsAFailureType,
  statusIsFinalized,
  statusIsRunningOrQueued,
} from "@/routes/tasks/types";

import { EditableNodeTitle } from "../components/EditableNodeTitle";
import { NodeActionMenu } from "../NodeActionMenu";
import { WorkflowBlockIcon } from "../WorkflowBlockIcon";
import { workflowBlockTitle } from "../types";
import { MicroDropdown } from "./MicroDropdown";

interface Transmutations {
  blockTitle: string;
  self: string;
  others: {
    label: string;
    reason: string;
    nodeName: string;
  }[];
}

interface Props {
  blockLabel: string; // today, this + wpid act as the identity of a block
  disabled?: boolean;
  editable: boolean;
  extraActions?: React.ReactNode;
  nodeId: string;
  totpIdentifier: string | null;
  totpUrl: string | null;
  transmutations?: Transmutations;
  type: WorkflowBlockType;
}

type Payload = Record<string, unknown> & {
  block_labels: string[];
  block_outputs: Record<string, unknown>;
  browser_session_id: string | null;
  extra_http_headers: Record<string, string> | null;
  max_screenshot_scrolls: number | null;
  parameters: Record<string, unknown>;
  proxy_location: ProxyLocation;
  totp_identifier: string | null;
  totp_url: string | null;
  webhook_url: string | null;
  workflow_id: string;
  code_gen: boolean | null;
};

const getPayload = (opts: {
  blockLabel: string;
  blockOutputs: Record<string, unknown>;
  browserSessionId: string | null;
  debugSessionId: string;
  codeGen: boolean | null;
  parameters: Record<string, unknown>;
  totpIdentifier: string | null;
  totpUrl: string | null;
  workflowPermanentId: string;
  workflowSettings: WorkflowSettingsState;
}): Payload | null => {
  const webhook_url = opts.workflowSettings.webhookCallbackUrl.trim();

  let extraHttpHeaders = null;

  try {
    extraHttpHeaders =
      opts.workflowSettings.extraHttpHeaders === null
        ? null
        : typeof opts.workflowSettings.extraHttpHeaders === "object"
          ? opts.workflowSettings.extraHttpHeaders
          : JSON.parse(opts.workflowSettings.extraHttpHeaders);
  } catch (e: unknown) {
    toast({
      variant: "warning",
      title: "Extra HTTP Headers",
      description: "Invalid extra HTTP Headers JSON",
    });
  }

  if (!opts.browserSessionId) {
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
      description: `Browser session ID found: ${opts.browserSessionId}`,
    });
  }

  const payload: Payload = {
    block_labels: [opts.blockLabel],
    block_outputs: opts.blockOutputs,
    browser_session_id: opts.browserSessionId,
    debug_session_id: opts.debugSessionId,
    code_gen: opts.codeGen,
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
  extraActions,
  nodeId,
  totpIdentifier,
  totpUrl,
  transmutations,
  type,
}: Props) {
  const log = useLogging();
  const {
    blockLabel: urlBlockLabel,
    workflowPermanentId,
    workflowRunId,
  } = useParams();
  const blockOutputsStore = useBlockOutputStore();
  const debugStore = useDebugStore();
  const recordingStore = useRecordingStore();
  const { closeWorkflowPanel } = useWorkflowPanelStore();
  const workflowSettingsStore = useWorkflowSettingsStore();
  const [label, setLabel] = useNodeLabelChangeHandler({
    id: nodeId,
    initialValue: blockLabel,
  });
  const blockTitle = workflowBlockTitle[type];
  const deleteNodeCallback = useDeleteNodeCallback();
  const transmuteNodeCallback = useTransmuteNodeCallback();
  const toggleScriptForNodeCallback = useToggleScriptForNodeCallback();
  const credentialGetter = useCredentialGetter();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const location = useLocation();
  const isDebuggable = debuggableWorkflowBlockTypes.has(type);
  const isScriptable = scriptableWorkflowBlockTypes.has(type);
  const { data: workflowRun } = useWorkflowRunQuery();
  const workflowRunIsRunningOrQueued =
    workflowRun && statusIsRunningOrQueued(workflowRun);
  const { data: debugSession } = useDebugSessionQuery({
    workflowPermanentId,
  });
  const { data: workflow } = useWorkflowQuery({
    workflowPermanentId,
  });
  const saveWorkflow = useWorkflowSave();

  const thisBlockIsPlaying =
    workflowRunIsRunningOrQueued &&
    urlBlockLabel !== undefined &&
    urlBlockLabel === blockLabel;

  const thisBlockIsTargetted =
    urlBlockLabel !== undefined && urlBlockLabel === blockLabel;

  const isRecording = recordingStore.isRecording;

  const [workflowRunStatus, setWorkflowRunStatus] = useState(
    workflowRun?.status,
  );
  const { getAutoplay, setAutoplay } = useAutoplayStore();

  useEffect(() => {
    if (!debugSession) {
      return;
    }

    const details = getAutoplay();

    if (
      workflowPermanentId === details.wpid &&
      blockLabel === details.blockLabel
    ) {
      setAutoplay(null, null);
      setTimeout(() => {
        runBlock.mutateAsync({ codeGen: true });
      }, 100);
    }

    // on mount
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debugSession]);

  useEffect(() => {
    setWorkflowRunStatus(workflowRun?.status);
  }, [workflowRun, setWorkflowRunStatus]);

  useOnChange(workflowRunStatus, (newValue, oldValue) => {
    if (!thisBlockIsTargetted) {
      return;
    }

    if (newValue !== oldValue && oldValue && newValue === Status.Completed) {
      queryClient.invalidateQueries({
        queryKey: ["block-outputs", workflowPermanentId],
      });
    }
  });

  useEffect(() => {
    if (!workflowRun || !workflowPermanentId || !workflowRunId) {
      return;
    }

    if (
      workflowRunId === workflowRun?.workflow_run_id &&
      statusIsFinalized(workflowRun)
    ) {
      // navigate(`/workflows/${workflowPermanentId}/debug`);

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
    queryClient,
    urlBlockLabel,
    navigate,
    workflowPermanentId,
    workflowRun,
    workflowRunId,
  ]);

  const runBlock = useMutation({
    mutationFn: async (opts?: { codeGen: boolean }) => {
      closeWorkflowPanel();

      await saveWorkflow.mutateAsync();

      if (!workflowPermanentId) {
        log.error("Run block: there is no workflowPermanentId");
        toast({
          variant: "destructive",
          title: "Failed to start workflow block run",
          description: "There is no workflowPermanentId",
        });
        return;
      }

      if (!debugSession) {
        // TODO: kind of redundant; investigate if this is necessary; either
        // Sentry's log should output to the console, or Sentry should just
        // gather native console.error output.
        console.error("Run block: there is no debug session, yet");
        log.error("Run block: there is no debug session, yet");
        toast({
          variant: "destructive",
          title: "Failed to start workflow block run",
          description: "There is no debug session, yet",
        });
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
        blockOutputs:
          blockOutputsStore.getOutputsWithOverrides(workflowPermanentId),
        browserSessionId: debugSession.browser_session_id,
        debugSessionId: debugSession.debug_session_id,
        codeGen: opts?.codeGen ?? false,
        parameters,
        totpIdentifier,
        totpUrl,
        workflowPermanentId,
        workflowSettings: workflowSettingsStore,
      });

      if (!body) {
        log.error("Run block: could not construct run payload", {
          workflowPermanentId,
          blockLabel,
          debugSessionId: debugSession.debug_session_id,
          browserSessionId: debugSession.browser_session_id,
        });
        toast({
          variant: "destructive",
          title: "Failed to start workflow block run",
          description: "Could not construct run payload",
        });
        return;
      }

      log.info("Run block: sending run payload", {
        workflowPermanentId,
        blockLabel,
        debugSessionId: debugSession.debug_session_id,
        browserSessionId: debugSession.browser_session_id,
      });

      return await client.post<Payload, { data: { run_id: string } }>(
        "/run/workflows/blocks",
        body,
      );
    },
    onSuccess: (response) => {
      if (!response) {
        log.error("Run block: no response", {
          workflowPermanentId,
          blockLabel,
          debugSessionId: debugSession?.debug_session_id,
          browserSessionId: debugSession?.browser_session_id,
        });
        toast({
          variant: "destructive",
          title: "Failed to start workflow block run",
          description: "No response",
        });
        return;
      }

      log.info("Run block: run started", {
        workflowPermanentId,
        blockLabel,
        debugSessionId: debugSession?.debug_session_id,
        browserSessionId: debugSession?.browser_session_id,
        runId: response.data.run_id,
      });

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
      log.error("Run block: error", {
        workflowPermanentId,
        blockLabel,
        debugSessionId: debugSession?.debug_session_id,
        browserSessionId: debugSession?.browser_session_id,
        error,
        detail,
      });
      toast({
        variant: "destructive",
        title: "Failed to start workflow block run",
        description: detail ?? error.message,
      });
    },
  });

  const cancelBlock = useMutation({
    mutationFn: async () => {
      if (!debugSession) {
        log.error("Cancel block: missing debug session", {
          workflowPermanentId,
          blockLabel,
        });
        toast({
          variant: "destructive",
          title: "Failed to cancel workflow block run",
          description: "Missing debug session",
        });
        return;
      }

      const browserSessionId = debugSession.browser_session_id;
      const client = await getClient(credentialGetter);
      return client
        .post(`/runs/${browserSessionId}/workflow_run/${workflowRunId}/cancel/`)
        .then((response) => response.data);
    },
    onSuccess: () => {
      log.info("Cancel block: canceled", {
        workflowPermanentId,
        blockLabel,
        debugSessionId: debugSession?.debug_session_id,
        browserSessionId: debugSession?.browser_session_id,
      });
      toast({
        variant: "success",
        title: "Workflow Canceled",
        description: "The workflow has been successfully canceled.",
      });
    },
    onError: (error: AxiosError) => {
      const detail = (error.response?.data as { detail?: string })?.detail;
      log.error("Cancel block: error", {
        workflowPermanentId,
        blockLabel,
        debugSessionId: debugSession?.debug_session_id,
        browserSessionId: debugSession?.browser_session_id,
        error,
        detail,
      });
      toast({
        variant: "destructive",
        title: "Error",
        description: detail ?? error.message,
      });
    },
  });

  const handleOnPlay = () => {
    const numBlocksInWorkflow = (workflow?.workflow_definition.blocks ?? [])
      .length;

    runBlock.mutate({ codeGen: numBlocksInWorkflow === 1 });
  };

  const handleOnCancel = () => {
    cancelBlock.mutate();
  };

  const isRunning = workflowRun ? statusIsRunningOrQueued(workflowRun) : false;
  const createdAt = toDate(workflowRun?.created_at ?? "", null);
  const finishedAt = toDate(workflowRun?.finished_at ?? "", null);
  const dt = finishedAt
    ? formatDate(finishedAt)
    : createdAt
      ? formatDate(createdAt)
      : null;

  return (
    <>
      {thisBlockIsTargetted ? (
        <div className="flex w-full animate-[auto-height_1s_ease-in-out_forwards] items-center justify-between overflow-hidden pb-4 pt-1">
          {isRunning ? (
            <div>
              <ReloadIcon className="animate-spin" />
            </div>
          ) : null}
          {dt ? <div className="text-sm opacity-70">{dt}</div> : <span />}
          <div>
            <StatusBadge status={workflowRun?.status ?? "pending"} />
          </div>
        </div>
      ) : null}

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

            {transmutations && transmutations.others.length ? (
              <div className="flex items-center gap-1">
                <span className="text-xs text-slate-400">
                  {transmutations.blockTitle}
                </span>
                <NoticeMe trigger="viewport">
                  <MicroDropdown
                    selections={[
                      transmutations.self,
                      ...transmutations.others.map((t) => t.label),
                    ]}
                    selected={transmutations.self}
                    onChange={(label) => {
                      const transmutation = transmutations.others.find(
                        (t) => t.label === label,
                      );

                      if (!transmutation) {
                        return;
                      }

                      transmuteNodeCallback(nodeId, transmutation.nodeName);
                    }}
                  />
                </NoticeMe>
              </div>
            ) : (
              <span className="text-xs text-slate-400">{blockTitle}</span>
            )}
          </div>
        </div>
        <div className="pointer-events-auto ml-auto flex items-center gap-2">
          {extraActions}
          {thisBlockIsPlaying && (
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
              disabled={workflowRunIsRunningOrQueued}
              className={cn("rounded p-1 disabled:opacity-50", {
                "hover:bg-muted": workflowRunIsRunningOrQueued,
              })}
            >
              {runBlock.isPending ? (
                <ReloadIcon className="size-6 animate-spin" />
              ) : (
                <PlayIcon
                  className={cn("size-6", {
                    "pointer-events-none fill-gray-500 text-gray-500":
                      workflowRunIsRunningOrQueued ||
                      !workflowPermanentId ||
                      debugSession === undefined ||
                      isRecording,
                  })}
                  onClick={() => {
                    handleOnPlay();
                  }}
                />
              )}
            </button>
          )}
          {disabled ? null : (
            <div>
              <div
                className={cn("rounded p-1 hover:bg-muted", {
                  "pointer-events-none opacity-50":
                    workflowRunIsRunningOrQueued,
                })}
              >
                <NodeActionMenu
                  isScriptable={isScriptable}
                  onDelete={() => {
                    deleteNodeCallback(nodeId);
                  }}
                  onShowScript={() =>
                    toggleScriptForNodeCallback({ id: nodeId, show: true })
                  }
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
