import { ReactFlowProvider } from "@xyflow/react";
import { useParams, useSearchParams } from "react-router-dom";
import { useEffect } from "react";
import { usePostHog } from "posthog-js/react";
import { useWorkflowQuery } from "../hooks/useWorkflowQuery";
import {
  getElements,
  upgradeWorkflowBlocksV1toV2,
} from "./workflowEditorUtils";
import { LogoMinimized } from "@/components/LogoMinimized";
import { WorkflowSettings } from "../types/workflowTypes";
import { useGlobalWorkflowsQuery } from "../hooks/useGlobalWorkflowsQuery";
import { useBlockOutputStore } from "@/store/BlockOutputStore";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { getInitialParameters } from "./utils";
import { Workspace } from "./Workspace";
import { ProductTour } from "@/components/onboarding/ProductTour";
import { useProductTourShortcut } from "@/hooks/useProductTourShortcut";
import { useMountEffect } from "@/hooks/useMountEffect";

function WorkflowEditor() {
  const { workflowPermanentId } = useParams();
  const [searchParams] = useSearchParams();
  const posthog = usePostHog();
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

  useMountEffect(() => {
    const via = searchParams.get("via");
    if (via) {
      posthog?.capture("copilot.discover.started", { entry_point: via });
    }
  });

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

  // Auto-upgrade v1 workflows to v2 by assigning sequential next_block_label values
  const workflowVersion = workflow.workflow_definition.version ?? 1;
  const blocksToRender =
    workflowVersion < 2
      ? upgradeWorkflowBlocksV1toV2(workflow.workflow_definition.blocks)
      : workflow.workflow_definition.blocks;

  const settings: WorkflowSettings = {
    persistBrowserSession: workflow.persist_browser_session,
    browserProfileId: workflow.browser_profile_id ?? null,
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
      <div className="relative flex flex-1">
        <ReactFlowProvider>
          <Workspace
            key={workflowPermanentId}
            initialEdges={elements.edges}
            initialNodes={elements.nodes}
            initialTitle={workflow.title}
            showBrowser={false}
            workflow={workflow}
          />
        </ReactFlowProvider>
      </div>
      <ProductTour />
    </div>
  );
}

export { WorkflowEditor };
