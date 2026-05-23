import { ResetIcon } from "@radix-ui/react-icons";
import { ControlButton } from "@xyflow/react";

import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useWorkflowHistoryAccessStore } from "@/store/WorkflowHistoryAccessStore";
import { useRecordingStore } from "@/store/useRecordingStore";

import { CollapsibleControl } from "./CollapsibleControl";
import { useUndoRedoShortcutLabels } from "./useUndoRedoShortcutLabels";

export function RedoControl() {
  const canRedo = useWorkflowHistoryAccessStore((s) => s.canRedo);
  const onRedo = useWorkflowHistoryAccessStore((s) => s.redo);
  const isRecording = useRecordingStore((s) => s.isRecording);
  const { redoShortcutLabel } = useUndoRedoShortcutLabels();

  const disabled = !canRedo || isRecording;

  return (
    <CollapsibleControl show={canRedo}>
      <TooltipProvider delayDuration={100}>
        <Tooltip>
          <TooltipTrigger asChild>
            <div>
              <ControlButton
                onClick={() => {
                  if (disabled) return;
                  onRedo();
                }}
                aria-label="Redo"
                disabled={disabled}
              >
                <ResetIcon className="size-4 -scale-x-100" />
              </ControlButton>
            </div>
          </TooltipTrigger>
          <TooltipContent side="right" className="z-[9999]">
            Redo ({redoShortcutLabel})
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>
    </CollapsibleControl>
  );
}
