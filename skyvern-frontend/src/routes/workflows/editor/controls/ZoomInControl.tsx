import { ControlButton, useReactFlow } from "@xyflow/react";

import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

export function ZoomInControl() {
  const { zoomIn } = useReactFlow();
  const label = "Zoom in";

  return (
    <TooltipProvider delayDuration={100}>
      <Tooltip>
        <TooltipTrigger asChild>
          <div>
            <ControlButton
              onClick={() => zoomIn({ duration: 200 })}
              aria-label={label}
            >
              <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
                <path d="M32 18.133H18.133V32h-4.266V18.133H0v-4.266h13.867V0h4.266v13.867H32z" />
              </svg>
            </ControlButton>
          </div>
        </TooltipTrigger>
        <TooltipContent side="right" className="z-[9999]">
          <div className="flex flex-col">
            <span>{label}</span>
            <span className="text-[0.65rem] opacity-70">Cmd / Ctrl +</span>
          </div>
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
