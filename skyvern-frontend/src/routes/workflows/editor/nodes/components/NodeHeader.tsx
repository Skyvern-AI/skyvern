import { AxiosError } from "axios";
import {
  ChevronDownIcon,
  ChevronUpIcon,
  PlayIcon,
  ReloadIcon,
  StopIcon,
} from "@radix-ui/react-icons";
import { useEffect, useRef, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useReactFlow } from "@xyflow/react";

import { getClient } from "@/api/AxiosClient";
import {
  ProxyLocation,
  Status,
  type DebugLoginBlockCompatibilityResponse,
} from "@/api/types";
import { NoticeMe } from "@/components/NoticeMe";
import { StatusBadge } from "@/components/StatusBadge";
import { toast } from "@/components/ui/use-toast";
import { useLogging } from "@/hooks/useLogging";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useWorkflowStudioEnabled } from "@/hooks/useWorkflowStudioEnabled";
import { useOnChange } from "@/hooks/useOnChange";
import { useAutoplayStore } from "@/store/useAutoplayStore";

import { useNodeLabelChangeHandler } from "@/routes/workflows/hooks/useLabelChangeHandler";
import { useDuplicateNodeCallback } from "@/routes/workflows/hooks/useDuplicateNodeCallback";
import { useRequestDeleteNodeCallback } from "@/routes/workflows/hooks/useRequestDeleteNodeCallback";
import { useTransmuteNodeCallback } from "@/routes/workflows/hooks/useTransmuteNodeCallback";
import { useToggleScriptForNodeCallback } from "@/routes/workflows/hooks/useToggleScriptForNodeCallback";
import { useBrowserSessionRateLimit } from "@/routes/workflows/hooks/useBrowserSessionRateLimit";
import { useCredentialsQuery } from "@/routes/workflows/hooks/useCredentialsQuery";
import { useDebugSessionQuery } from "@/routes/workflows/hooks/useDebugSessionQuery";
import { useWorkflowQuery } from "@/routes/workflows/hooks/useWorkflowQuery";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import { DebugSessionProfileIncompatibleDialog } from "@/routes/workflows/debugger/DebugSessionProfileIncompatibleDialog";
import {
  DEBUG_LOGIN_GATE_CREDENTIALS_PAGE_SIZE,
  decideDebugLoginPlayGate,
  gateActionFromBackendCompatibility,
  type CredentialsLoadState,
  type DebugSessionProfileIncompatibilityReason,
} from "@/routes/workflows/debugger/debugSessionProfileCompatibility";
import {
  debuggableWorkflowBlockTypes,
  scriptableWorkflowBlockTypes,
  type WorkflowBlockType,
  type WorkflowApiResponse,
  type WorkflowParameter,
  type Parameter,
} from "@/routes/workflows/types/workflowTypes";
import { getBlockParameterDependencies } from "@/routes/workflows/editor/debugger/getBlockParameterDependencies";
import { findWorkflowBlockByLabel } from "@/routes/workflows/workflowBlockUtils";
import { getInitialValues } from "@/routes/workflows/utils";
import { useDebuggerLastRunValuesStore } from "@/store/DebuggerLastRunValuesStore";
import { useBlockOutputStore } from "@/store/BlockOutputStore";
import { useDebugStore } from "@/store/useDebugStore";
import {
  STUDIO_PANES_PARAM,
  resolveOpenPanes,
  withPanesOpen,
} from "@/routes/workflows/studio/panes";
import { useRecordingStore } from "@/store/useRecordingStore";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";
import { useWorkflowSave } from "@/store/WorkflowHasChangesStore";
import {
  useWorkflowSettingsStore,
  type WorkflowSettingsState,
} from "@/store/WorkflowSettingsStore";
import { getJsonParseErrorDetail } from "@/util/jsonParseError";
import { cn, formatDate, toDate } from "@/util/utils";
import {
  statusIsAFailureType,
  statusIsFinalized,
  statusIsRunningOrQueued,
} from "@/routes/tasks/types";

import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

import { EditableNodeTitle } from "../components/EditableNodeTitle";
import { NodeActionMenu } from "../NodeActionMenu";
import { WorkflowBlockIcon } from "../WorkflowBlockIcon";
import { workflowBlockTitle } from "../types";
import { MicroDropdown } from "./MicroDropdown";
import { BlockParametersDialog } from "./BlockParametersDialog";
import type { AppNode } from "..";
import { getWorkflowErrors } from "../../workflowEditorUtils";
import { NodeGripHandle } from "./NodeGripHandle";
import {
  getDragGateReason,
  isDragGatedByMode,
} from "../../sortable/dragModeGate";
import { useIsCanvasLocked } from "../../controls/useIsCanvasLocked";
import { isBlockFinallyGated } from "../../sortable/finallyBlockGate";
import { collapsibleWorkflowBlockTypes } from "../../collapse/collapsibleBlockTypes";
import {
  useIsBlockCollapsed,
  useNodeCollapseStore,
} from "../../collapse/useNodeCollapseStore";
import { useWorkflowEditorMode } from "../../hooks/useWorkflowEditorMode";
import { useWorkflowScopeReadOnly } from "../../WorkflowScopeContext";

class ValidationFailureError extends Error {
  readonly isValidationFailure = true;
  constructor() {
    super("workflow validation failed");
    this.name = "ValidationFailureError";
  }
}

function isWorkflowParameter(param: Parameter): param is WorkflowParameter {
  return (
    param?.parameter_type === "workflow" &&
    "workflow_parameter_type" in param &&
    typeof param.workflow_parameter_type === "string" &&
    param.workflow_parameter_type.length > 0
  );
}

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
  blockTitle?: string;
  disabled?: boolean;
  editable: boolean;
  extraActions?: React.ReactNode;
  // Driven by useSortable in the parent HOC; defaults to false so this
  // file ships a static affordance with no behavioural change at call sites.
  isDragging?: boolean;
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
      description: `Invalid extra HTTP Headers JSON: ${getJsonParseErrorDetail(
        String(opts.workflowSettings.extraHttpHeaders ?? ""),
        e,
      )}`,
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
  blockTitle: blockTitleOverride,
  disabled = false,
  editable,
  extraActions,
  isDragging = false,
  nodeId,
  totpIdentifier,
  totpUrl,
  transmutations,
  type,
}: Props) {
  const log = useLogging();
  const mode = useWorkflowEditorMode();
  const {
    blockLabel: urlBlockLabel,
    workflowPermanentId,
    workflowRunId,
  } = useParams();
  const blockOutputsStore = useBlockOutputStore();
  const debugStore = useDebugStore();
  const recordingStore = useRecordingStore();
  const isCollapsed = useIsBlockCollapsed(blockLabel);
  const toggleBlockCollapsed = useNodeCollapseStore((s) => s.toggleBlock);
  const isCollapsible = collapsibleWorkflowBlockTypes.has(type);
  const { closeWorkflowPanel } = useWorkflowPanelStore();
  const workflowSettingsStore = useWorkflowSettingsStore();
  const [label, setLabel] = useNodeLabelChangeHandler({
    id: nodeId,
    initialValue: blockLabel,
  });
  const blockTitle = blockTitleOverride ?? workflowBlockTitle[type];
  const duplicateNodeCallback = useDuplicateNodeCallback();
  const requestDeleteNodeCallback = useRequestDeleteNodeCallback();
  const transmuteNodeCallback = useTransmuteNodeCallback();
  const toggleScriptForNodeCallback = useToggleScriptForNodeCallback();
  const credentialGetter = useCredentialGetter();
  const navigate = useNavigate();
  const studioEnabled = useWorkflowStudioEnabled();
  const queryClient = useQueryClient();
  const location = useLocation();
  const isDebuggable = debuggableWorkflowBlockTypes.has(type);
  const isScriptable = scriptableWorkflowBlockTypes.has(type);
  const { data: workflowRun } = useWorkflowRunQuery();
  const workflowRunIsRunningOrQueued =
    workflowRun && statusIsRunningOrQueued(workflowRun);
  const { isRateLimited } = useBrowserSessionRateLimit(workflowPermanentId);
  const { data: debugSession } = useDebugSessionQuery({
    workflowPermanentId,
    isRateLimited,
  });
  const { data: workflow } = useWorkflowQuery({
    workflowPermanentId,
  });
  const saveWorkflow = useWorkflowSave();
  const reactFlow = useReactFlow<AppNode>();

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
  const {
    data: credentials,
    isLoading: credentialsIsLoading,
    isError: credentialsIsError,
    refetch: refetchCredentials,
  } = useCredentialsQuery({
    enabled: type === "login" && !!debugSession,
    page_size: DEBUG_LOGIN_GATE_CREDENTIALS_PAGE_SIZE,
  });
  const [profileIncompatibilityReason, setProfileIncompatibilityReason] =
    useState<DebugSessionProfileIncompatibilityReason | null>(null);
  const compatibilityLookupInFlightRef = useRef(false);
  const [showParamsDialog, setShowParamsDialog] = useState(false);
  const [parametersToPrompt, setParametersToPrompt] = useState<
    WorkflowParameter[]
  >([]);
  const [currentParamValues, setCurrentParamValues] = useState<
    Record<string, unknown>
  >({});
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
      // navigate(`/workflows/${workflowPermanentId}/build`);

      if (statusIsAFailureType(workflowRun)) {
        toast({
          variant: "destructive",
          title: `Agent Block ${urlBlockLabel}: ${workflowRun.status}`,
          description: `Reason: ${workflowRun.failure_reason}`,
        });
      } else if (statusIsFinalized(workflowRun)) {
        toast({
          variant: "success",
          title: `Agent Block ${urlBlockLabel}: ${workflowRun.status}`,
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
    mutationFn: async (opts?: {
      codeGen: boolean;
      parameterOverrides?: Record<string, unknown>;
    }) => {
      closeWorkflowPanel();

      // Compute errors against the full graph so per-block validators that
      // reference siblings/edges (loops, conditionals, validation block
      // ordering) behave correctly, then keep only the ones tagged with this
      // block's label so unrelated unfinished nodes don't block running this
      // one. Filter relies on the implicit contract that every validator
      // formats errors as `${label}: ${message}` - if that ever drifts,
      // errors for this block would silently slip past this gate.
      const allErrors = getWorkflowErrors(reactFlow.getNodes());
      const labelPrefix = `${blockLabel}:`;
      const blockErrors = allErrors.filter((e) => e.startsWith(labelPrefix));
      if (blockErrors.length > 0) {
        toast({
          variant: "destructive",
          title: "Can not run block because of errors:",
          description: (
            <div className="space-y-2">
              {blockErrors.map((error) => (
                <p key={error}>{error}</p>
              ))}
            </div>
          ),
        });
        // Throw a typed error so React Query routes to onError (not
        // onSuccess) without firing the generic "Failed to start" toast.
        throw new ValidationFailureError();
      }

      await saveWorkflow.mutateAsync();

      if (!workflowPermanentId) {
        log.error("Run block: there is no workflowPermanentId");
        toast({
          variant: "destructive",
          title: "Failed to start agent block run",
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
          title: "Failed to start agent block run",
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

      const lastRunValues = workflowPermanentId
        ? useDebuggerLastRunValuesStore
            .getState()
            .getLastRunValues(workflowPermanentId)
        : null;

      const parameters = getInitialValues(
        location,
        workflowParameters ?? [],
        lastRunValues,
      );

      // Merge with parameter overrides if provided
      const mergedParameters = opts?.parameterOverrides
        ? { ...parameters, ...opts.parameterOverrides }
        : parameters;

      const client = await getClient(credentialGetter, "sans-api-v1");

      const body = getPayload({
        blockLabel,
        blockOutputs:
          blockOutputsStore.getOutputsWithOverrides(workflowPermanentId),
        browserSessionId: debugSession.browser_session_id,
        debugSessionId: debugSession.debug_session_id,
        codeGen: opts?.codeGen ?? false,
        parameters: mergedParameters,
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
          title: "Failed to start agent block run",
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
          title: "Failed to start agent block run",
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
        title: "Agent block run started",
        description: "The agent block run has been started successfully",
      });

      if (studioEnabled) {
        // One navigation carries the pane state (current panes plus Run and
        // Browser); other query params intentionally reset for the fresh run.
        const liveSearch = window.location.search || location.search;
        const panes = withPanesOpen(resolveOpenPanes(liveSearch), [
          "run",
          "browser",
        ]);
        const search = new URLSearchParams({
          wr: response.data.run_id,
          bl: label,
        });
        search.set(STUDIO_PANES_PARAM, panes.join(","));
        navigate(`/workflows/${workflowPermanentId}/studio?${search}`);
      } else {
        navigate(
          `/workflows/${workflowPermanentId}/${response.data.run_id}/${label}/build`,
        );
      }
    },
    onError: (error: AxiosError | ValidationFailureError) => {
      // The block-validation gate threw a typed error and already showed
      // its own toast; don't stack the generic "Failed to start" on top.
      if (error instanceof ValidationFailureError) {
        return;
      }
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
        title: "Failed to start agent block run",
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
          title: "Failed to cancel agent block run",
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
        title: "Agent Canceled",
        description: "The agent has been successfully canceled.",
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

  const proceedWithPlay = () => {
    const blocks = workflow?.workflow_definition?.blocks ?? [];
    const numBlocksInWorkflow = blocks.length;

    // Get workflow parameters using type guard for proper type narrowing
    const workflowParameters = (
      workflow?.workflow_definition?.parameters ?? []
    ).filter(isWorkflowParameter);

    if (workflowParameters.length > 0) {
      const lastRunValues = workflowPermanentId
        ? useDebuggerLastRunValuesStore
            .getState()
            .getLastRunValues(workflowPermanentId)
        : null;
      const currentValues = getInitialValues(
        location,
        workflowParameters,
        lastRunValues,
      );
      const block = findWorkflowBlockByLabel(blocks, blockLabel);
      const parametersToRun = getBlockParameterDependencies(
        block ?? undefined,
        workflowParameters,
      );

      if (parametersToRun.length > 0) {
        setCurrentParamValues(currentValues);
        setParametersToPrompt(parametersToRun);
        setShowParamsDialog(true);
        return;
      }

      runBlock.mutate({
        codeGen: numBlocksInWorkflow === 1,
        parameterOverrides: currentValues,
      });
      return;
    }

    runBlock.mutate({ codeGen: numBlocksInWorkflow === 1 });
  };

  const handleOnPlay = async () => {
    // Fail-closed pre-flight: a still-loading credentials list (or an
    // errored / pagination-missed credential) can produce a null credential
    // profile that the compatibility check would misread as "compatible",
    // silently bypassing the mismatch modal. Block the play with a recovery
    // affordance until we have a definitive answer.
    const credentialsState: CredentialsLoadState = credentialsIsLoading
      ? "loading"
      : credentialsIsError
        ? "error"
        : "ready";
    const blocks = workflow?.workflow_definition?.blocks ?? [];
    const block = findWorkflowBlockByLabel(blocks, blockLabel);

    const gate = decideDebugLoginPlayGate({
      blockType: type,
      hasDebugSession: !!debugSession,
      credentialsState,
      block,
      credentials,
      pbsBrowserProfileId: debugSession?.pbs_browser_profile_id ?? null,
    });

    if (gate.kind === "block-loading") {
      toast({
        variant: "warning",
        title: "Loading credentials",
        description:
          "Credentials are still loading. Please try again in a moment.",
      });
      return;
    }

    if (gate.kind === "block-retry" && gate.reason === "credentials-error") {
      // Settled-in-error leaves the gate permanently blocked until React
      // Query refetches. Trigger a refetch and tell the user to try again;
      // the next click re-evaluates against the fresh query state.
      void refetchCredentials();
      toast({
        variant: "warning",
        title: "Couldn't load credentials",
        description: "Retrying — try Play again once the toast clears.",
      });
      return;
    }

    if (gate.kind === "block-retry" && gate.reason === "credential-not-found") {
      // The credential the LoginBlock references is not in the bounded
      // useCredentialsQuery window (paginated past it, deleted, etc.).
      // Refetching the same page can't fix that, so ask the backend to
      // resolve the credential through the org-scoped lookup the run path
      // uses, and apply that verdict directly.
      if (!workflowPermanentId) {
        toast({
          variant: "warning",
          title: "Couldn't verify credential",
          description:
            "Workflow context not ready. Refresh the page and try again.",
        });
        return;
      }
      if (compatibilityLookupInFlightRef.current) {
        toast({
          variant: "warning",
          title: "Checking compatibility…",
          description: "Already verifying the credential — please wait.",
        });
        return;
      }
      compatibilityLookupInFlightRef.current = true;
      try {
        const client = await getClient(credentialGetter, "sans-api-v1");
        const response = await client.get<
          unknown,
          { data: DebugLoginBlockCompatibilityResponse }
        >(`/debug-session/${workflowPermanentId}/login-block-compatibility`, {
          params: { block_label: blockLabel },
        });
        const verdict = gateActionFromBackendCompatibility(response.data);
        if (verdict.kind === "show-modal") {
          setProfileIncompatibilityReason(verdict.reason);
          return;
        }
        if (verdict.kind === "block-retry") {
          toast({
            variant: "warning",
            title: "Couldn't verify credential",
            description:
              "The credential lookup returned an unexpected response. Try Play again, or refresh the page.",
          });
          return;
        }
        proceedWithPlay();
        return;
      } catch (error) {
        log.error("Compatibility lookup failed", {
          workflowPermanentId,
          blockLabel,
          error,
        });
        toast({
          variant: "warning",
          title: "Couldn't verify credential",
          description:
            "The credential lookup didn't complete. Try Play again, or refresh the page.",
        });
        return;
      } finally {
        compatibilityLookupInFlightRef.current = false;
      }
    }

    if (gate.kind === "show-modal") {
      setProfileIncompatibilityReason(gate.reason);
      return;
    }

    proceedWithPlay();
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

  const isReadOnlyScope = useWorkflowScopeReadOnly();
  const isCanvasLocked = useIsCanvasLocked();
  const dragGatedByMode = isDragGatedByMode({ isRecording, isCanvasLocked });
  const duplicateDisabledReason = isBlockFinallyGated(
    blockLabel,
    workflowSettingsStore.finallyBlockLabel,
  )
    ? "Finally block must run last"
    : null;

  // Read-only canvases (compare/diff) drop the grip entirely - the handle
  // is inert there, so a faded button is just visual noise.
  let gripHandle: React.ReactNode = null;
  if (!isReadOnlyScope) {
    const gripHandleElement = (
      <NodeGripHandle
        isDragging={isDragging}
        disabled={dragGatedByMode}
        blockLabel={blockLabel}
      />
    );
    const dragGateReason = dragGatedByMode
      ? getDragGateReason({ isRecording, isCanvasLocked })
      : null;
    gripHandle = dragGatedByMode ? (
      <TooltipProvider delayDuration={200}>
        <Tooltip>
          <TooltipTrigger asChild>
            <span tabIndex={0} className="inline-flex">
              {gripHandleElement}
            </span>
          </TooltipTrigger>
          {dragGateReason && <TooltipContent>{dragGateReason}</TooltipContent>}
        </Tooltip>
      </TooltipProvider>
    ) : (
      gripHandleElement
    );
  }

  // Recording mid-collapse would change captured DOM, so freeze only on
  // isRecording. Read-only renders (comparison canvases) must not mutate
  // the workflow's persisted collapse state — `isReadOnlyScope` (read
  // above) is true when FlowRenderer is mounted with `readOnly`, in
  // which case the toggle is disabled so a compare-canvas click cannot
  // persist collapse state into the editor view the user returns to.
  const collapseToggleGated = isRecording || isReadOnlyScope;
  const collapseLabel = isCollapsed ? "Expand block" : "Collapse block";
  const collapseToggleButton =
    isCollapsible &&
    (mode === "build" ||
      type === "for_loop" ||
      type === "while_loop" ||
      type === "conditional") ? (
      <TooltipProvider delayDuration={300}>
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              type="button"
              aria-label={collapseLabel}
              aria-expanded={!isCollapsed}
              disabled={collapseToggleGated}
              onClick={(e) => {
                e.stopPropagation();
                if (collapseToggleGated) return;
                toggleBlockCollapsed(
                  workflowPermanentId ?? "__global__",
                  blockLabel,
                );
              }}
              className={cn("nodrag nopan rounded p-1 hover:bg-muted", {
                "pointer-events-none opacity-50": collapseToggleGated,
              })}
            >
              {isCollapsed ? (
                <ChevronDownIcon className="size-5" />
              ) : (
                <ChevronUpIcon className="size-5" />
              )}
            </button>
          </TooltipTrigger>
          <TooltipContent>{collapseLabel}</TooltipContent>
        </Tooltip>
      </TooltipProvider>
    ) : null;

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

      <header className="group !mt-0 flex h-[2.75rem] justify-between gap-2">
        <div
          className={cn("flex min-w-0 gap-2", {
            "opacity-50": thisBlockIsPlaying,
          })}
        >
          {!isReadOnlyScope &&
            (isBlockFinallyGated(
              blockLabel,
              workflowSettingsStore.finallyBlockLabel,
            ) ? (
              <TooltipProvider delayDuration={300}>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <div>
                      <NodeGripHandle blockLabel={blockLabel} disabled />
                    </div>
                  </TooltipTrigger>
                  <TooltipContent>
                    Finally block runs last - reorder to a different position
                  </TooltipContent>
                </Tooltip>
              </TooltipProvider>
            ) : (
              gripHandle
            ))}
          <div className="flex h-[2.75rem] w-[2.75rem] items-center justify-center rounded border border-slate-600">
            <WorkflowBlockIcon workflowBlockType={type} className="size-6" />
          </div>
          <div className="flex min-w-0 flex-col gap-1">
            <EditableNodeTitle
              value={blockLabel}
              editable={editable}
              onChange={setLabel}
              titleClassName="text-base"
              inputClassName="text-base"
            />

            <div className="flex items-center gap-2">
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
                <span
                  className="min-w-0 flex-1 truncate text-xs text-slate-400"
                  title={blockTitle}
                >
                  {blockTitle}
                </span>
              )}
              {workflowSettingsStore.finallyBlockLabel === blockLabel && (
                <span className="rounded bg-amber-600/20 px-1.5 py-0.5 text-[10px] font-medium text-amber-400">
                  Runs on any outcome
                </span>
              )}
            </div>
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
          {(debugStore.isDebugMode || debugStore.blockRunsEnabled) &&
            isDebuggable && (
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
                      void handleOnPlay();
                    }}
                  />
                )}
              </button>
            )}
          {collapseToggleButton}
          {disabled ? null : (
            <div>
              <div
                className={cn("rounded p-1 hover:bg-muted", {
                  "pointer-events-none opacity-50":
                    workflowRunIsRunningOrQueued,
                })}
              >
                <NodeActionMenu
                  duplicateDisabledReason={duplicateDisabledReason}
                  isDuplicable={
                    !isReadOnlyScope && Boolean(duplicateNodeCallback)
                  }
                  isScriptable={isScriptable}
                  isCanvasLocked={isCanvasLocked}
                  onDuplicate={
                    isReadOnlyScope || !duplicateNodeCallback
                      ? undefined
                      : () => {
                          duplicateNodeCallback(nodeId);
                        }
                  }
                  onDelete={() => {
                    requestDeleteNodeCallback(nodeId, blockLabel);
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

      <BlockParametersDialog
        open={showParamsDialog}
        onOpenChange={setShowParamsDialog}
        blockLabel={blockLabel}
        parameters={parametersToPrompt}
        initialValues={currentParamValues}
        onSubmit={(values) => {
          const numBlocksInWorkflow = (
            workflow?.workflow_definition.blocks ?? []
          ).length;
          runBlock.mutate(
            {
              codeGen: numBlocksInWorkflow === 1,
              parameterOverrides: values,
            },
            {
              onSuccess: () => {
                if (workflowPermanentId) {
                  useDebuggerLastRunValuesStore
                    .getState()
                    .setLastRunValues(workflowPermanentId, values);
                }
                // Close dialog on success - navigation also happens in mutation's onSuccess
                setShowParamsDialog(false);
              },
              // On error, dialog stays open so user can retry. Toast is shown by mutation's onError.
            },
          );
        }}
        isLoading={runBlock.isPending}
      />
      <DebugSessionProfileIncompatibleDialog
        open={profileIncompatibilityReason !== null}
        reason={profileIncompatibilityReason}
        onContinue={() => {
          setProfileIncompatibilityReason(null);
          proceedWithPlay();
        }}
        onCancel={() => setProfileIncompatibilityReason(null)}
      />
    </>
  );
}

export { NodeHeader };
