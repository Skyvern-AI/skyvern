import { useEffect } from "react";
import { useMountEffect } from "@/hooks/useMountEffect";
import { useBlockScriptsQuery } from "@/routes/workflows/hooks/useBlockScriptsQuery";
import { useBlockScriptStore } from "@/store/BlockScriptStore";
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
  const workflowChangesStore = useWorkflowHasChangesStore();
  const blockScriptStore = useBlockScriptStore();

  const { data: workflow, isLoading } = useWorkflowQuery({
    workflowPermanentId,
  });

  const { data: globalWorkflows, isLoading: isGlobalWorkflowsLoading } =
    useGlobalWorkflowsQuery();

  const { data: blockScripts } = useBlockScriptsQuery({
    workflowPermanentId,
  });

  useMountEffect(() => {
    setCollapsed(true);
    workflowChangesStore.setHasChanges(false);
  });

  useEffect(() => {
    blockScriptStore.setScripts(blockScripts ?? {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [blockScripts]);

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
    useScriptCache: workflow.generate_script,
    scriptCacheKey: workflow.cache_key,
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
