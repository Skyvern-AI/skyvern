import { useIsSkyvernUser } from "@/hooks/useIsSkyvernUser";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

import { cn } from "@/util/utils";

function OrgWalled({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  const isSkyvernUser = useIsSkyvernUser();

  if (!isSkyvernUser) {
    return null;
  }

  // Wrap children with visual indication for org-walled features
  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <div
            className={cn(
              "relative rounded-md border-2 border-dashed border-yellow-400 p-2",
              className,
            )}
          >
            {children}
          </div>
        </TooltipTrigger>
        <TooltipContent>
          <p>This feature is only available to Skyvern organization members</p>
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

export { OrgWalled };
