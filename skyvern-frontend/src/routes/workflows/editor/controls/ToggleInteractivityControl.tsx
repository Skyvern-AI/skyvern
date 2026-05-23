import { LockClosedIcon, LockOpen2Icon } from "@radix-ui/react-icons";
import { ControlButton, useStore, useStoreApi } from "@xyflow/react";

import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

const interactiveSelector = (s: {
  nodesDraggable: boolean;
  nodesConnectable: boolean;
  elementsSelectable: boolean;
}) => s.nodesDraggable || s.nodesConnectable || s.elementsSelectable;

export function ToggleInteractivityControl() {
  const isInteractive = useStore(interactiveSelector);
  const store = useStoreApi();

  const onToggle = () => {
    store.setState({
      nodesDraggable: !isInteractive,
      nodesConnectable: !isInteractive,
      elementsSelectable: !isInteractive,
    });
  };

  const label = isInteractive ? "Lock canvas" : "Unlock canvas";

  return (
    <TooltipProvider delayDuration={100}>
      <Tooltip>
        <TooltipTrigger asChild>
          <div>
            <ControlButton onClick={onToggle} aria-label={label}>
              {isInteractive ? (
                <LockOpen2Icon className="size-4" />
              ) : (
                <LockClosedIcon className="size-4" />
              )}
            </ControlButton>
          </div>
        </TooltipTrigger>
        <TooltipContent side="right" className="z-[9999]">
          {label}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
