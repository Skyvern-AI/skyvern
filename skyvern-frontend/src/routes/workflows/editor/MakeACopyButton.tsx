import { CopyIcon, ReloadIcon } from "@radix-ui/react-icons";
import { useParams } from "react-router-dom";

import { Button } from "@/components/ui/button";

import { useCreateWorkflowMutation } from "../hooks/useCreateWorkflowMutation";
import { useGlobalWorkflowsQuery } from "../hooks/useGlobalWorkflowsQuery";
import { convert } from "./workflowEditorUtils";

function MakeACopyButton() {
  const { workflowPermanentId } = useParams();
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
