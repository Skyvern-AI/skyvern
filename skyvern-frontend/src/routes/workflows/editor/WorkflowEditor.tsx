import { ReactFlowProvider } from "@xyflow/react";
import { useParams } from "react-router-dom";
import { useEffect } from "react";
import { useWorkflowQuery } from "../hooks/useWorkflowQuery";
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
  const studioEnabled = useWorkflowStudioEnabled();
  const { data: workflow, isLoading } = useWorkflowQuery({
    workflowPermanentId,
  });

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
    if (workflow) {
      const initialParameters = getInitialParameters(workflow);
      setParameters(initialParameters);
    }
  }, [workflow, setParameters]);

  if (isLoading || isGlobalWorkflowsLoading) {
    return (
      <div className="flex h-screen w-full items-center justify-center">
        <div className="animate-pulse">
          <LogoMinimized />
        </div>
      </div>
    );
  }

  if (!workflow) {
    return null;
  }

  const isGlobalWorkflow = globalWorkflows?.some(
    (globalWorkflow) =>
      globalWorkflow.workflow_permanent_id === workflowPermanentId,
  );

  // getElements derives display routing (sequential defaulting + validation); the stored blocks are passed through unchanged.
  const blocksToRender = workflow.workflow_definition.blocks;

  const settings: WorkflowSettings = {
    persistBrowserSession: workflow.persist_browser_session,
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
    runSequentially: workflow.run_sequentially ?? false,
    sequentialKey: workflow.sequential_key ?? null,
    finallyBlockLabel:
      workflow.workflow_definition?.finally_block_label ?? null,
    workflowSystemPrompt:
      workflow.workflow_definition?.workflow_system_prompt ?? null,
    errorCodeMapping: workflow.workflow_definition?.error_code_mapping ?? null,
  };

  const elements = getElements(blocksToRender, settings, !isGlobalWorkflow);

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
