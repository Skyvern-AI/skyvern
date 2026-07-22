import { CopyIcon, ReloadIcon } from "@radix-ui/react-icons";
import { useWorkflowPermanentId } from "@/routes/workflows/WorkflowPermanentIdContext";

import { Button } from "@/components/ui/button";

import { useCreateWorkflowMutation } from "../hooks/useCreateWorkflowMutation";
import { useGlobalWorkflowsQuery } from "../hooks/useGlobalWorkflowsQuery";
import { convert } from "./workflowEditorUtils";

function MakeACopyButton() {
  const workflowPermanentId = useWorkflowPermanentId();
  const { data: globalWorkflows } = useGlobalWorkflowsQuery();
  const createWorkflowMutation = useCreateWorkflowMutation();

  const handleClick = () => {
    const workflow = globalWorkflows?.find(
      (w) => w.workflow_permanent_id === workflowPermanentId,
    );
    if (!workflow) {
      return;
    }
    createWorkflowMutation.mutate(convert(workflow));
  };

  return (
    <Button size="lg" onClick={handleClick}>
      {createWorkflowMutation.isPending ? (
        <ReloadIcon className="mr-3 h-6 w-6 animate-spin" />
      ) : (
        <CopyIcon className="mr-3 h-6 w-6" />
      )}
      Make a Copy to Edit
    </Button>
  );
}

export { MakeACopyButton };
