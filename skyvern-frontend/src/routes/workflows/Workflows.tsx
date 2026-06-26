import { useWorkflowsDirectoryTree } from "@/hooks/useWorkflowsDirectoryTree";

import { WorkflowsFlat } from "./WorkflowsFlat";
import { WorkflowsTree } from "./WorkflowsTree";

function Workflows() {
  const directoryTreeEnabled = useWorkflowsDirectoryTree();
  return directoryTreeEnabled ? <WorkflowsTree /> : <WorkflowsFlat />;
}

export { Workflows };
