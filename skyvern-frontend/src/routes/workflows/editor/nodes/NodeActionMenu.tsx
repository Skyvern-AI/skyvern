import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { DotsHorizontalIcon } from "@radix-ui/react-icons";
import { OrgWalled } from "@/components/Orgwalled";

type Props = {
  isScriptable?: boolean;
  onDelete: () => void;
  onShowScript?: () => void;
};

function NodeActionMenu({
  isScriptable = false,
  onDelete,
  onShowScript,
}: Props) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <DotsHorizontalIcon className="h-6 w-6 cursor-pointer" />
      </DropdownMenuTrigger>
      <DropdownMenuContent>
        <DropdownMenuLabel>Block Actions</DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          onSelect={() => {
            onDelete();
          }}
        >
          Delete Block
        </DropdownMenuItem>
        {isScriptable && (
          <OrgWalled className="p-0">
            {onShowScript && (
              <DropdownMenuItem
                onSelect={() => {
                  onShowScript();
                }}
              >
                Show Script
              </DropdownMenuItem>
            )}
          </OrgWalled>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export { NodeActionMenu };
