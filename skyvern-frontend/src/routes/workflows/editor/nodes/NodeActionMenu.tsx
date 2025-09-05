import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { DotsHorizontalIcon } from "@radix-ui/react-icons";

type Props = {
  isDeletable?: boolean;
  isScriptable?: boolean;
  showScriptText?: string;
  onDelete?: () => void;
  onShowScript?: () => void;
};

function NodeActionMenu({
  isDeletable = true,
  isScriptable = false,
  showScriptText,
  onDelete,
  onShowScript,
}: Props) {
  if (!isDeletable && !isScriptable) {
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
        {isDeletable && (
          <DropdownMenuItem
            onSelect={() => {
              onDelete?.();
            }}
          >
            Delete Block
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
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export { NodeActionMenu };
