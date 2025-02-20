import { Skeleton } from "@/components/ui/skeleton";
import { useGlobalWorkflowsQuery } from "../workflows/hooks/useGlobalWorkflowsQuery";
import { useNavigate } from "react-router-dom";
import { WorkflowTemplateCard } from "./WorkflowTemplateCard";
import testImg from "@/assets/promptBoxBg.png";
import { TEMPORARY_TEMPLATE_IMAGES } from "./TemporaryTemplateImages";

function WorkflowTemplates() {
  const { data: workflowTemplates, isLoading } = useGlobalWorkflowsQuery();
  const navigate = useNavigate();

  if (isLoading) {
    return (
      <div className="flex gap-6">
        <Skeleton className="h-48 w-56 rounded-xl" />
        <Skeleton className="h-48 w-56 rounded-xl" />
        <Skeleton className="h-48 w-56 rounded-xl" />
      </div>
    );
  }

  if (!workflowTemplates) {
    return null;
  }

  return (
    <div className="space-y-5">
      <h1 className="text-xl">Explore Workflows</h1>
      <div className="flex gap-6">
        {workflowTemplates.map((workflow) => {
          return (
            <WorkflowTemplateCard
              key={workflow.workflow_permanent_id}
              title={workflow.title}
              image={
                TEMPORARY_TEMPLATE_IMAGES[workflow.workflow_permanent_id] ??
                testImg
              }
              onClick={() => {
                navigate(`/workflows/${workflow.workflow_permanent_id}/edit`);
              }}
            />
          );
        })}
      </div>
    </div>
  );
}

export { WorkflowTemplates };
