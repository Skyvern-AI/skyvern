import { Skeleton } from "@/components/ui/skeleton";
import { useGlobalWorkflowsQuery } from "../workflows/hooks/useGlobalWorkflowsQuery";
import { useNavigate } from "react-router-dom";
import { WorkflowTemplateCard } from "./WorkflowTemplateCard";
import testImg from "@/assets/promptBoxBg.png";
import { TEMPORARY_TEMPLATE_IMAGES } from "./TemporaryTemplateImages";
import {
  Carousel,
  CarouselContent,
  CarouselItem,
  CarouselNext,
  CarouselPrevious,
} from "@/components/ui/carousel";

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
      <Carousel
        opts={{
          align: "start",
          loop: true,
        }}
        className="w-full"
      >
        <CarouselContent className="-ml-6">
          {workflowTemplates.map((workflow) => (
            <CarouselItem
              key={workflow.workflow_permanent_id}
              className="basis-1/5 pl-6"
            >
              <WorkflowTemplateCard
                title={workflow.title}
                image={
                  TEMPORARY_TEMPLATE_IMAGES[workflow.workflow_permanent_id] ??
                  testImg
                }
                onClick={() => {
                  navigate(
                    `/workflows/${workflow.workflow_permanent_id}/debug`,
                  );
                }}
              />
            </CarouselItem>
          ))}
        </CarouselContent>
        <CarouselPrevious className="-left-4" />
        <CarouselNext className="-right-4" />
      </Carousel>
    </div>
  );
}

export { WorkflowTemplates };
