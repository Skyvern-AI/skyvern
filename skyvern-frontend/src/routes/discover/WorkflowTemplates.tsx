import { LightningBoltIcon } from "@radix-ui/react-icons";

import { Skeleton } from "@/components/ui/skeleton";
import { useGlobalWorkflowsQuery } from "../workflows/hooks/useGlobalWorkflowsQuery";
import { useNavigate } from "react-router-dom";
import { WorkflowTemplateCard } from "./WorkflowTemplateCard";
import {
  Carousel,
  CarouselContent,
  CarouselItem,
  CarouselNext,
  CarouselPrevious,
} from "@/components/ui/carousel";

const VISIBLE_SLOTS = 5;

function WorkflowTemplates() {
  const { data: workflowTemplates, isLoading } = useGlobalWorkflowsQuery();
  const navigate = useNavigate();

  if (isLoading) {
    return (
      <div className="space-y-5">
        <h1 className="flex items-center gap-2 text-2xl">
          <LightningBoltIcon className="size-6" />
          Explore Workflows
        </h1>
        <div className="flex gap-6">
          <Skeleton className="h-52 w-56 rounded-xl" />
          <Skeleton className="h-52 w-56 rounded-xl" />
          <Skeleton className="h-52 w-56 rounded-xl" />
        </div>
      </div>
    );
  }

  if (!workflowTemplates || workflowTemplates.length === 0) {
    return (
      <div className="space-y-5">
        <h1 className="flex items-center gap-2 text-2xl">
          <LightningBoltIcon className="size-6" />
          Explore Workflows
        </h1>
        <div className="flex flex-col items-center gap-3 rounded-xl border border-dashed border-border bg-muted/40 py-12">
          <LightningBoltIcon className="size-8 text-muted-foreground" />
          <div className="text-base font-medium">No templates yet</div>
          <p className="max-w-sm text-center text-sm text-muted-foreground">
            Workflow templates will appear here once published. In the meantime,
            describe a task above to generate a workflow from scratch.
          </p>
        </div>
      </div>
    );
  }

  const showArrows = workflowTemplates.length > VISIBLE_SLOTS;

  return (
    <div className="space-y-5">
      <h1 className="flex items-center gap-2 text-2xl">
        <LightningBoltIcon className="size-6" />
        Explore Workflows
      </h1>
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
                onClick={() => {
                  navigate(
                    `/workflows/${workflow.workflow_permanent_id}/build`,
                  );
                }}
              />
            </CarouselItem>
          ))}
        </CarouselContent>
        {showArrows && (
          <>
            <CarouselPrevious className="-left-4" />
            <CarouselNext className="-right-4" />
          </>
        )}
      </Carousel>
    </div>
  );
}

export { WorkflowTemplates };
