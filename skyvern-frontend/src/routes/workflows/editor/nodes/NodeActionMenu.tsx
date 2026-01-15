import { DotsHorizontalIcon, Pencil2Icon } from "@radix-ui/react-icons";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useRecordingStore } from "@/store/useRecordingStore";

type Props = {
  isDeletable?: boolean;
  isRenameable?: boolean;
  isScriptable?: boolean;
  showScriptText?: string;
  onDelete?: () => void;
  onRename?: () => void;
  onShowScript?: () => void;
};

function NodeActionMenu({
  isDeletable = true,
  isRenameable = true,
  isScriptable = false,
  showScriptText,
  onDelete,
  onRename,
  onShowScript,
}: Props) {
  const recordingStore = useRecordingStore();
  const isRecording = recordingStore.isRecording;

  if (!isDeletable && !isScriptable && !isRenameable) {
    return null;
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <DotsHorizontalIcon className="h-6 w-6 cursor-pointer" />
      </DropdownMenuTrigger>
      <DropdownMenuContent>
        <DropdownMenuLabel>Block Actions</DropdownMenuLabel>
        <DropdownMenuSeparator />
        {isRenameable && onRename && (
          <DropdownMenuItem
            disabled={isRecording}
            onSelect={() => {
              onRename();
            }}
          >
            <Pencil2Icon className="mr-2 h-4 w-4" />
            Rename Block
          </DropdownMenuItem>
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
        {isDeletable && (
          <DropdownMenuItem
            disabled={isRecording}
            onSelect={() => {
              onDelete?.();
            }}
            className="text-destructive focus:text-destructive"
          >
            Delete Block
          </DropdownMenuItem>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export { NodeActionMenu };
