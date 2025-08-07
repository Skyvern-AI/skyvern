import { useMountEffect } from "@/hooks/useMountEffect";
import { useSidebarStore } from "@/store/SidebarStore";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import { ReactFlowProvider } from "@xyflow/react";
import { useParams } from "react-router-dom";
import { useWorkflowQuery } from "../hooks/useWorkflowQuery";
import { FlowRenderer } from "./FlowRenderer";
import { getElements } from "./workflowEditorUtils";
import { LogoMinimized } from "@/components/LogoMinimized";
import { WorkflowSettings } from "../types/workflowTypes";
import { useGlobalWorkflowsQuery } from "../hooks/useGlobalWorkflowsQuery";
import { getInitialParameters } from "./utils";

function WorkflowEditor() {
  const { workflowPermanentId } = useParams();
  const setCollapsed = useSidebarStore((state) => {
    return state.setCollapsed;
  });
  const setHasChanges = useWorkflowHasChangesStore(
    (state) => state.setHasChanges,
  );

  const { data: workflow, isLoading } = useWorkflowQuery({
    workflowPermanentId,
  });

  const { data: globalWorkflows, isLoading: isGlobalWorkflowsLoading } =
    useGlobalWorkflowsQuery();

  useMountEffect(() => {
    setCollapsed(true);
    setHasChanges(false);
  });

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
    useScriptCache: workflow.use_cache,
  };

  const elements = getElements(
    workflow.workflow_definition.blocks,
    settings,
    !isGlobalWorkflow,
  );

  return (
    <div className="relative flex h-screen w-full">
      <ReactFlowProvider>
        <FlowRenderer
          initialEdges={elements.edges}
          initialNodes={elements.nodes}
          initialParameters={getInitialParameters(workflow)}
          initialTitle={workflow.title}
          workflow={workflow}
        />
      </ReactFlowProvider>
    </div>
  );
}

export { WorkflowEditor };
