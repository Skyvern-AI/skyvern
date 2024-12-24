import { AspectRatio } from "@/components/ui/aspect-ratio";
import { Skeleton } from "@/components/ui/skeleton";

function WorkflowRunOverviewSkeleton() {
  return (
    <div className="flex h-[42rem] gap-6">
      <div className="w-2/3 space-y-4">
        <AspectRatio ratio={16 / 9}>
          <Skeleton className="h-full w-full" />
        </AspectRatio>
        <div className="h-[10rem]">
          <Skeleton className="h-full w-full" />
        </div>
      </div>
      <div className="w-1/3">
        <Skeleton className="h-full w-full" />
      </div>
    </div>
  );
}

export { WorkflowRunOverviewSkeleton };
