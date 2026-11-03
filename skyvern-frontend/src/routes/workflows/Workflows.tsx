import { CreatorDirectoryBoundary } from "@/components/CreatorDirectoryBoundary";
import { useWorkflowsDirectoryTree } from "@/hooks/useWorkflowsDirectoryTree";

import { WorkflowsFlat } from "./WorkflowsFlat";
import { WorkflowsTree } from "./WorkflowsTree";

function Workflows() {
  const directoryTreeEnabled = useWorkflowsDirectoryTree();
  return (
    <CreatorDirectoryBoundary>
      {directoryTreeEnabled ? <WorkflowsTree /> : <WorkflowsFlat />}
    </CreatorDirectoryBoundary>
  );
}

export { Workflows };
