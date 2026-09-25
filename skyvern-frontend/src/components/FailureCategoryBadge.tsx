import type { FailureCategory } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { getFailureCategoryDisplay } from "@/util/failureCategoryDisplay";

type Props = {
  failureCategory: Array<FailureCategory> | null;
};

function FailureCategoryBadge({ failureCategory }: Props) {
  const primary = failureCategory?.[0];
  // failure_category is untyped JSON on the backend and can carry model output, so guard at runtime.
  if (typeof primary?.category !== "string" || !primary.category.trim()) {
    return null;
  }
  const { label, description } = getFailureCategoryDisplay(primary);
  // Self-contained provider so the badge works outside the studio's provider; tabIndex keeps
  // the explanation reachable on keyboard focus, not hover only.
  return (
    <TooltipProvider delayDuration={200}>
      <Tooltip>
        <TooltipTrigger asChild>
          <span
            tabIndex={0}
            className="inline-flex w-fit rounded-md outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
          >
            <Badge variant="destructive">{label}</Badge>
          </span>
        </TooltipTrigger>
        <TooltipContent side="bottom" className="max-w-xs">
          {description}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

export { FailureCategoryBadge };
