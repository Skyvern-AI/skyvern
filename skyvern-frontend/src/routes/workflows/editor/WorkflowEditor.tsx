import { ReactFlowProvider } from "@xyflow/react";
import { useParams, useSearchParams } from "react-router-dom";
import { useEffect } from "react";
import { useWorkflowQuery } from "../hooks/useWorkflowQuery";
import { useWorkflowRunWithWorkflowQuery } from "../hooks/useWorkflowRunWithWorkflowQuery";
import { getElements } from "./workflowEditorUtils";
import { LogoMinimized } from "@/components/LogoMinimized";
import { WorkflowSettings } from "../types/workflowTypes";
import { useGlobalWorkflowsQuery } from "../hooks/useGlobalWorkflowsQuery";
import { useBlockOutputStore } from "@/store/BlockOutputStore";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { getInitialParameters } from "./utils";
import { StudioShell } from "../studio/StudioShell";
import { Workspace } from "./Workspace";
import { ProductTour } from "@/components/onboarding/ProductTour";
import { useProductTourShortcut } from "@/hooks/useProductTourShortcut";
import { useMountEffect } from "@/hooks/useMountEffect";
import { useWorkflowStudioEnabled } from "@/hooks/useWorkflowStudioEnabled";
import { useViaEntryPointCapture } from "../hooks/useViaEntryPointCapture";

function WorkflowEditor() {
  const { workflowPermanentId } = useParams();
  const [searchParams] = useSearchParams();
  const studioEnabled = useWorkflowStudioEnabled();
  const {
    data: fetchedWorkflow,
    isLoading,
    isError: workflowQueryFailed,
  } = useWorkflowQuery({
    workflowPermanentId,
  });

  // Runs outlive their agent: the workflow GET 404s once the agent is deleted,
  // but a ?wr= deep link can still be served from the run's embedded workflow
  // snapshot (the same query the run panes use), in a read-only degraded mode.
  const deepLinkRunId = searchParams.get("wr");
  const { data: fallbackRun, isLoading: fallbackRunIsLoading } =
    useWorkflowRunWithWorkflowQuery(
      studioEnabled && deepLinkRunId
        ? { workflowRunId: deepLinkRunId }
        : undefined,
    );
  const deletedWorkflowSnapshot =
    studioEnabled && workflowQueryFailed && fallbackRun?.workflow?.deleted_at
      ? fallbackRun.workflow
      : undefined;
  const effectiveWorkflow = fetchedWorkflow ?? deletedWorkflowSnapshot;

  const { data: globalWorkflows, isLoading: isGlobalWorkflowsLoading } =
    useGlobalWorkflowsQuery();

  const setParameters = useWorkflowParametersStore(
    (state) => state.setParameters,
  );

  const blockOutputStore = useBlockOutputStore();

  useProductTourShortcut();

  useMountEffect(() => blockOutputStore.reset());

  useViaEntryPointCapture();

  useEffect(() => {
    if (effectiveWorkflow) {
      const initialParameters = getInitialParameters(effectiveWorkflow);
      setParameters(initialParameters);
    }
  }, [effectiveWorkflow, setParameters]);

  const awaitingRunFallback =
    studioEnabled &&
    workflowQueryFailed &&
    Boolean(deepLinkRunId) &&
    fallbackRunIsLoading;
  if (isLoading || isGlobalWorkflowsLoading || awaitingRunFallback) {
    return (
      <div className="flex h-screen w-full items-center justify-center">
        <div className="animate-pulse">
          <LogoMinimized />
        </div>
      </div>
    );
  }

  if (!effectiveWorkflow) {
    return null;
  }
  const workflow = effectiveWorkflow;
  const workflowDeleted = Boolean(workflow.deleted_at);

  const isGlobalWorkflow = globalWorkflows?.some(
    (globalWorkflow) =>
      globalWorkflow.workflow_permanent_id === workflowPermanentId,
  );

  // getElements derives display routing (sequential defaulting + validation); the stored blocks are passed through unchanged.
  const blocksToRender = workflow.workflow_definition.blocks;

  const settings: WorkflowSettings = {
    persistBrowserSession: workflow.persist_browser_session,
    pinSavedSessionIp: workflow.pin_saved_session_ip ?? false,
    browserProfileId: workflow.browser_profile_id ?? null,
    browserProfileKey: workflow.browser_profile_key ?? null,
    proxyLocation: workflow.proxy_location,
    webhookCallbackUrl: workflow.webhook_callback_url,
    model: workflow.model,
    maxScreenshotScrolls: workflow.max_screenshot_scrolls,
    maxElapsedTimeMinutes: workflow.max_elapsed_time_minutes ?? null,
    extraHttpHeaders: workflow.extra_http_headers
      ? JSON.stringify(workflow.extra_http_headers)
      : null,
    cdpConnectHeaders: workflow.cdp_connect_headers
      ? JSON.stringify(workflow.cdp_connect_headers)
      : null,
    runWith: workflow.run_with ?? "agent",
    codeVersion: workflow.code_version ?? null,
    scriptCacheKey: workflow.cache_key,
    aiFallback: workflow.ai_fallback ?? true,
    enableSelfHealing: workflow.enable_self_healing ?? false,
    runSequentially: workflow.run_sequentially ?? false,
    sequentialKey: workflow.sequential_key ?? null,
    finallyBlockLabel:
      workflow.workflow_definition?.finally_block_label ?? null,
    workflowSystemPrompt:
      workflow.workflow_definition?.workflow_system_prompt ?? null,
    errorCodeMapping: workflow.workflow_definition?.error_code_mapping ?? null,
  };

  const elements = getElements(
    blocksToRender,
    settings,
    !isGlobalWorkflow && !workflowDeleted,
  );

  return (
    <div className="relative flex h-screen w-full flex-col">
      {elements.validationError ? (
        <div
          role="alert"
          className="z-10 border-b border-amber-700/40 bg-amber-950/50 px-4 py-2 text-sm text-amber-200"
        >
          <strong className="font-semibold">
            Workflow validation warning:
          </strong>{" "}
          {elements.validationError.message}
        </div>
      ) : null}
      <div className="relative flex min-h-0 flex-1">
        <ReactFlowProvider>
          {studioEnabled ? (
            <StudioShell
              key={workflowPermanentId}
              initialEdges={elements.edges}
              initialNodes={elements.nodes}
              initialTitle={workflow.title}
              workflow={workflow}
            />
          ) : (
            <Workspace
              key={workflowPermanentId}
              initialEdges={elements.edges}
              initialNodes={elements.nodes}
              initialTitle={workflow.title}
              showBrowser={false}
              workflow={workflow}
            />
          )}
        </ReactFlowProvider>
      </div>
      <ProductTour />
    </div>
  );
}

export { WorkflowEditor };
