import { DotsHorizontalIcon } from "@radix-ui/react-icons";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useRecordingStore } from "@/store/useRecordingStore";

type Props = {
  duplicateDisabledReason?: string | null;
  isDeletable?: boolean;
  isDuplicable?: boolean;
  isScriptable?: boolean;
  isCanvasLocked?: boolean;
  showScriptText?: string;
  onDelete?: () => void;
  onDuplicate?: () => void;
  onShowScript?: () => void;
};

function NodeActionMenu({
  duplicateDisabledReason = null,
  isDeletable = true,
  isDuplicable = true,
  isScriptable = false,
  isCanvasLocked = false,
  showScriptText,
  onDelete,
  onDuplicate,
  onShowScript,
}: Props) {
  const recordingStore = useRecordingStore();
  const isRecording = recordingStore.isRecording;
  const deleteGated = isRecording || isCanvasLocked;
  const deleteGateReason = isRecording
    ? "Stop recording to delete blocks"
    : isCanvasLocked
      ? "Unlock canvas to delete blocks"
      : null;
  const duplicateGated =
    isRecording || isCanvasLocked || Boolean(duplicateDisabledReason);
  const duplicateGateReason = isRecording
    ? "Stop recording to duplicate blocks"
    : isCanvasLocked
      ? "Unlock canvas to duplicate blocks"
      : duplicateDisabledReason;

  if (!isDeletable && !isDuplicable && !isScriptable) {
    return null;
  }

  const duplicateItem =
    isDuplicable && onDuplicate ? (
      <DropdownMenuItem
        disabled={duplicateGated}
        onSelect={(event) => {
          if (duplicateGated) {
            event.preventDefault();
            return;
          }
          onDuplicate();
        }}
      >
        Duplicate Block
      </DropdownMenuItem>
    ) : null;

  const deleteItem = isDeletable ? (
    <DropdownMenuItem
      disabled={deleteGated}
      onSelect={(event) => {
        if (deleteGated) {
          event.preventDefault();
          return;
        }
        onDelete?.();
      }}
    >
      Delete Block
    </DropdownMenuItem>
  ) : null;

  return (
    <DropdownMenu modal={false}>
      <TooltipProvider delayDuration={300}>
        <Tooltip>
          <TooltipTrigger asChild>
            <DropdownMenuTrigger asChild>
              <button
                type="button"
                aria-label="Block actions"
                className="nodrag nopan flex rounded focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              >
                <DotsHorizontalIcon className="h-6 w-6" aria-hidden />
              </button>
            </DropdownMenuTrigger>
          </TooltipTrigger>
          <TooltipContent>Block actions</TooltipContent>
        </Tooltip>
      </TooltipProvider>
      <DropdownMenuContent align="end" collisionPadding={8}>
        <DropdownMenuLabel>Block Actions</DropdownMenuLabel>
        <DropdownMenuSeparator />
        {duplicateItem && duplicateGated && duplicateGateReason ? (
          <TooltipProvider delayDuration={200}>
            <Tooltip>
              <TooltipTrigger asChild>
                <span className="block">{duplicateItem}</span>
              </TooltipTrigger>
              <TooltipContent side="left">{duplicateGateReason}</TooltipContent>
            </Tooltip>
          </TooltipProvider>
        ) : (
          duplicateItem
        )}
        {deleteItem && deleteGated && deleteGateReason ? (
          <TooltipProvider delayDuration={200}>
            <Tooltip>
              <TooltipTrigger asChild>
                <span className="block">{deleteItem}</span>
              </TooltipTrigger>
              <TooltipContent side="left">{deleteGateReason}</TooltipContent>
            </Tooltip>
          </TooltipProvider>
        ) : (
          deleteItem
        )}
        {isScriptable && onShowScript && (
          <DropdownMenuItem
            onSelect={() => {
              onShowScript();
            }}
          >
            {showScriptText ?? "Show Code"}
          </DropdownMenuItem>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export { NodeActionMenu };
