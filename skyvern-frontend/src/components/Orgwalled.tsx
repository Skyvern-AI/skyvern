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
  hideTooltipContent,
}: {
  children: React.ReactNode;
  className?: string;
  hideTooltipContent?: boolean;
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
              "relative rounded-md border-2 border-dashed border-yellow-400 p-2 transition-all duration-100 ease-linear hover:border-transparent hover:p-0",
              className,
            )}
          >
            {children}
          </div>
        </TooltipTrigger>
        {!hideTooltipContent && (
          <TooltipContent>
            <p>
              This feature is only available to Skyvern organization members
            </p>
          </TooltipContent>
        )}
      </Tooltip>
    </TooltipProvider>
  );
}

export { OrgWalled };
