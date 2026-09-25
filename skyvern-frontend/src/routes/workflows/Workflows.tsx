import { useWorkflowsDirectoryTree } from "@/hooks/useWorkflowsDirectoryTree";
import { usePageSlots } from "@/store/PageSlots";

import { WorkflowsFlat } from "./WorkflowsFlat";
import { WorkflowsTree } from "./WorkflowsTree";

function Workflows() {
  const directoryTreeEnabled = useWorkflowsDirectoryTree();
  const { workflowCreatorDirectory: CreatorDirectory } = usePageSlots();
  const list = directoryTreeEnabled ? <WorkflowsTree /> : <WorkflowsFlat />;
  return CreatorDirectory ? <CreatorDirectory>{list}</CreatorDirectory> : list;
}

export { Workflows };
