import { useEffect } from "react";
import { useParams } from "react-router-dom";
import { ReactFlowProvider } from "@xyflow/react";

import { useWorkflowQuery } from "../hooks/useWorkflowQuery";
import { WorkflowSettings } from "../types/workflowTypes";
import { getElements } from "@/routes/workflows/editor/workflowEditorUtils";
import { getInitialParameters } from "@/routes/workflows/editor/utils";
import { Workspace } from "@/routes/workflows/editor/Workspace";
import { useDebugSessionBlockOutputsQuery } from "../hooks/useDebugSessionBlockOutputsQuery";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { useBlockOutputStore } from "@/store/BlockOutputStore";

function Debugger() {
  const { workflowPermanentId } = useParams();
  const { data: workflow } = useWorkflowQuery({
    workflowPermanentId,
  });
  const { data: outputParameters } = useDebugSessionBlockOutputsQuery({
    workflowPermanentId,
  });

  const setParameters = useWorkflowParametersStore(
    (state) => state.setParameters,
  );

  const setBlockOutputs = useBlockOutputStore((state) => state.setOutputs);

  useEffect(() => {
    if (workflow) {
      const initialParameters = getInitialParameters(workflow);
      setParameters(initialParameters);
    }
  }, [workflow, setParameters]);

  useEffect(() => {
    if (!outputParameters) {
      return;
    }

    const blockOutputs = Object.entries(outputParameters).reduce<{
      [k: string]: Record<string, unknown>;
    }>((acc, [blockLabel, outputs]) => {
      acc[blockLabel] = outputs ?? null;
      return acc;
    }, {});

    setBlockOutputs(blockOutputs);
  }, [outputParameters, setBlockOutputs]);

  if (!workflow) {
    return null;
  }

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
    aiFallback: workflow.ai_fallback,
  };

  const elements = getElements(
    workflow.workflow_definition.blocks,
    settings,
    true,
  );

  return (
    <div className="relative flex h-screen w-full">
      <ReactFlowProvider>
        <Workspace
          initialEdges={elements.edges}
          initialNodes={elements.nodes}
          initialTitle={workflow.title}
          showBrowser={true}
          workflow={workflow}
        />
      </ReactFlowProvider>
    </div>
  );
}

export { Debugger };
