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

export function UndoControl() {
  const canUndo = useWorkflowHistoryAccessStore((s) => s.canUndo);
  const onUndo = useWorkflowHistoryAccessStore((s) => s.undo);
  const isRecording = useRecordingStore((s) => s.isRecording);
  const { undoShortcutLabel } = useUndoRedoShortcutLabels();

  const disabled = !canUndo || isRecording;

  return (
    <CollapsibleControl show={canUndo}>
      <TooltipProvider delayDuration={100}>
        <Tooltip>
          <TooltipTrigger asChild>
            <div>
              <ControlButton
                onClick={() => {
                  if (disabled) return;
                  onUndo();
                }}
                aria-label="Undo"
                disabled={disabled}
              >
                <ResetIcon className="size-4" />
              </ControlButton>
            </div>
          </TooltipTrigger>
          <TooltipContent side="right" className="z-[9999]">
            Undo ({undoShortcutLabel})
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>
    </CollapsibleControl>
  );
}
