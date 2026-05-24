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
  isDeletable?: boolean;
  isScriptable?: boolean;
  isCanvasLocked?: boolean;
  showScriptText?: string;
  onDelete?: () => void;
  onShowScript?: () => void;
};

function NodeActionMenu({
  isDeletable = true,
  isScriptable = false,
  isCanvasLocked = false,
  showScriptText,
  onDelete,
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

  if (!isDeletable && !isScriptable) {
    return null;
  }

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
      <DropdownMenuContent>
        <DropdownMenuLabel>Block Actions</DropdownMenuLabel>
        <DropdownMenuSeparator />
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
