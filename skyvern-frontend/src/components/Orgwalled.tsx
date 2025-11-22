import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useIsSkyzonaUser } from "@/hooks/useIsSkyzonaUser";

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
  const isSkyzonaUser = useIsSkyzonaUser();

  if (!isSkyzonaUser) {
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
              This feature is only available to Skyzona organization members
            </p>
          </TooltipContent>
        )}
      </Tooltip>
    </TooltipProvider>
  );
}

export { OrgWalled };
