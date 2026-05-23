import { ChevronDownIcon, ChevronUpIcon } from "@radix-ui/react-icons";

import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/util/utils";

type WorkflowHeaderCollapseTabProps = {
  collapsed: boolean;
  onToggle: () => void;
};

function WorkflowHeaderCollapseTab({
  collapsed,
  onToggle,
}: WorkflowHeaderCollapseTabProps) {
  const Icon = collapsed ? ChevronDownIcon : ChevronUpIcon;
  const label = collapsed ? "Expand header" : "Collapse header";

  return (
    <TooltipProvider delayDuration={300}>
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            aria-label={label}
            aria-expanded={!collapsed}
            onClick={onToggle}
            className={cn(
              "absolute -bottom-5 left-1/2 z-10 flex h-5 w-16 -translate-x-1/2 items-center justify-center rounded-b-md transition-colors duration-300 ease-out focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-500",
              collapsed
                ? "bg-foreground text-background hover:bg-foreground/90"
                : "bg-slate-elevation2 text-foreground hover:bg-slate-elevation3",
            )}
          >
            <Icon className="h-4 w-4" aria-hidden="true" />
          </button>
        </TooltipTrigger>
        <TooltipContent side="bottom" sideOffset={4}>
          {label}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

export { WorkflowHeaderCollapseTab };
