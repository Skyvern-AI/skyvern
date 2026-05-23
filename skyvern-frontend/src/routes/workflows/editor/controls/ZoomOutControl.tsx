import { ControlButton, useReactFlow } from "@xyflow/react";

import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

export function ZoomOutControl() {
  const { zoomOut } = useReactFlow();
  const label = "Zoom out";

  return (
    <TooltipProvider delayDuration={100}>
      <Tooltip>
        <TooltipTrigger asChild>
          <div>
            <ControlButton
              onClick={() => zoomOut({ duration: 200 })}
              aria-label={label}
            >
              <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 5">
                <path d="M0 0h32v4.2H0z" />
              </svg>
            </ControlButton>
          </div>
        </TooltipTrigger>
        <TooltipContent side="right" className="z-[9999]">
          <div className="flex flex-col">
            <span>{label}</span>
            <span className="text-[0.65rem] opacity-70">Cmd / Ctrl -</span>
          </div>
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
