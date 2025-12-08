import { useEffect } from "react";
import { useParams } from "react-router-dom";
import { ReactFlowProvider } from "@xyflow/react";

import { useWorkflowQuery } from "../hooks/useWorkflowQuery";
import { WorkflowSettings } from "../types/workflowTypes";
import {
  getElements,
  upgradeWorkflowBlocksV1toV2,
} from "@/routes/workflows/editor/workflowEditorUtils";
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

  // Auto-upgrade v1 workflows to v2 by assigning sequential next_block_label values
  const workflowVersion = workflow.workflow_definition.version ?? 1;
  const blocksToRender =
    workflowVersion < 2
      ? upgradeWorkflowBlocksV1toV2(workflow.workflow_definition.blocks)
      : workflow.workflow_definition.blocks;

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

  const elements = getElements(blocksToRender, settings, true);

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
