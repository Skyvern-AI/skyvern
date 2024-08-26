import { ReactFlowProvider } from "@xyflow/react";
import { useParams } from "react-router-dom";
import { useWorkflowQuery } from "../hooks/useWorkflowQuery";
import { FlowRenderer } from "./FlowRenderer";
import { getElements } from "./workflowEditorUtils";

function WorkflowEditor() {
  const { workflowPermanentId } = useParams();

  const { data: workflow, isLoading } = useWorkflowQuery({
    workflowPermanentId,
  });

  // TODO
  if (isLoading) {
    return (
      <div className="flex h-screen w-full items-center justify-center">
        Loading...
      </div>
    );
  }

  if (!workflow) {
    return null;
  }

  const elements = getElements(workflow.workflow_definition.blocks);

  return (
    <div className="h-screen w-full">
      <ReactFlowProvider>
        <FlowRenderer
          title={workflow.title}
          initialNodes={elements.nodes}
          initialEdges={elements.edges}
        />
      </ReactFlowProvider>
    </div>
  );
}

export { WorkflowEditor };
