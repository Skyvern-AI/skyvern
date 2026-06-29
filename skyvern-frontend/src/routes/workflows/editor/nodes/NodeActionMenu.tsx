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
      <DropdownMenuTrigger asChild>
        <DotsHorizontalIcon className="h-6 w-6 cursor-pointer" />
      </DropdownMenuTrigger>
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
