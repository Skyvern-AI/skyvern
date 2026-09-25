import { apiWorkflowToSettings } from "@/routes/workflows/editor/apiWorkflowToSettings";
import { useEffect } from "react";
import { useParams } from "react-router-dom";
import { ReactFlowProvider } from "@xyflow/react";

import { LogoMinimized } from "@/components/LogoMinimized";
import { useWorkflowQuery } from "../hooks/useWorkflowQuery";
import { useViaEntryPointCapture } from "../hooks/useViaEntryPointCapture";
import { getElements } from "@/routes/workflows/editor/workflowEditorUtils";
import { useHydrateWorkflowParameters } from "@/store/WorkflowHasChangesStore";
import { Workspace } from "@/routes/workflows/editor/Workspace";
import { useDebugSessionBlockOutputsQuery } from "../hooks/useDebugSessionBlockOutputsQuery";
import { useBlockOutputStore } from "@/store/BlockOutputStore";

function Debugger() {
  const { workflowPermanentId } = useParams();
  useViaEntryPointCapture();
  const { data: workflow, isLoading } = useWorkflowQuery({
    workflowPermanentId,
  });
  const { data: outputParameters } = useDebugSessionBlockOutputsQuery({
    workflowPermanentId,
  });

  const setBlockOutputs = useBlockOutputStore((state) => state.setOutputs);

  useHydrateWorkflowParameters(workflow, workflowPermanentId);

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

  if (isLoading) {
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

  // getElements derives display routing (sequential defaulting + validation); the stored blocks are passed through unchanged.
  const blocksToRender = workflow.workflow_definition.blocks;

  const settings = apiWorkflowToSettings(workflow);

  const elements = getElements(blocksToRender, settings, true);

  return (
    <div className="relative flex h-screen w-full">
      <ReactFlowProvider>
        <Workspace
          key={workflowPermanentId}
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
