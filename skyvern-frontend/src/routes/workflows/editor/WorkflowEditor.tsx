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
import { Workspace } from "./Workspace";
import { useMountEffect } from "@/hooks/useMountEffect";

function WorkflowEditor() {
  const { workflowPermanentId } = useParams();
  const { data: workflow, isLoading } = useWorkflowQuery({
    workflowPermanentId,
  });

  const { data: globalWorkflows, isLoading: isGlobalWorkflowsLoading } =
    useGlobalWorkflowsQuery();

  const setParameters = useWorkflowParametersStore(
    (state) => state.setParameters,
  );

  const blockOutputStore = useBlockOutputStore();

  useMountEffect(() => blockOutputStore.reset());

  useEffect(() => {
    if (workflow) {
      const initialParameters = getInitialParameters(workflow);
      setParameters(initialParameters);
    }
  }, [workflow, setParameters]);

  if (isLoading || isGlobalWorkflowsLoading) {
    return (
      <div className="flex h-screen w-full items-center justify-center">
        <LogoMinimized />
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

  const settings: WorkflowSettings = {
    persistBrowserSession: workflow.persist_browser_session,
    proxyLocation: workflow.proxy_location,
    webhookCallbackUrl: workflow.webhook_callback_url,
    model: workflow.model,
    maxScreenshotScrolls: workflow.max_screenshot_scrolls,
    extraHttpHeaders: workflow.extra_http_headers
      ? JSON.stringify(workflow.extra_http_headers)
      : null,
    runWith: workflow.run_with,
    scriptCacheKey: workflow.cache_key,
    aiFallback: workflow.ai_fallback ?? true,
    runSequentially: workflow.run_sequentially ?? false,
    sequentialKey: workflow.sequential_key ?? null,
  };

  const elements = getElements(
    workflow.workflow_definition.blocks,
    settings,
    !isGlobalWorkflow,
  );

  return (
    <div className="relative flex h-screen w-full">
      <ReactFlowProvider>
        <Workspace
          initialEdges={elements.edges}
          initialNodes={elements.nodes}
          initialTitle={workflow.title}
          showBrowser={false}
          workflow={workflow}
        />
      </ReactFlowProvider>
    </div>
  );
}

export { WorkflowEditor };
