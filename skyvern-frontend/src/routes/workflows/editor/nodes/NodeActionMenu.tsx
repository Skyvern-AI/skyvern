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

type Props = {
  demonstrateDisabledReason?: string | null;
  duplicateDisabledReason?: string | null;
  isDeletable?: boolean;
  isDuplicable?: boolean;
  isCanvasLocked?: boolean;
  onDelete?: () => void;
  onDemonstrate?: () => void;
  onDuplicate?: () => void;
};

function NodeActionMenu({
  demonstrateDisabledReason = null,
  duplicateDisabledReason = null,
  isDeletable = true,
  isDuplicable = true,
  isCanvasLocked = false,
  onDelete,
  onDemonstrate,
  onDuplicate,
}: Props) {
  const deleteGated = isCanvasLocked;
  const deleteGateReason = isCanvasLocked
    ? "Unlock canvas to delete blocks"
    : null;
  const duplicateGated = isCanvasLocked || Boolean(duplicateDisabledReason);
  const duplicateGateReason = isCanvasLocked
    ? "Unlock canvas to duplicate blocks"
    : duplicateDisabledReason;

  if (!isDeletable && !isDuplicable) {
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
        {onDemonstrate &&
          (demonstrateDisabledReason ? (
            <TooltipProvider delayDuration={200}>
              <Tooltip>
                <TooltipTrigger asChild>
                  <span className="block">
                    <DropdownMenuItem
                      disabled
                      onSelect={(event) => event.preventDefault()}
                    >
                      Demonstrate this block
                    </DropdownMenuItem>
                  </span>
                </TooltipTrigger>
                <TooltipContent side="left">
                  {demonstrateDisabledReason}
                </TooltipContent>
              </Tooltip>
            </TooltipProvider>
          ) : (
            <DropdownMenuItem onSelect={() => onDemonstrate()}>
              Demonstrate this block
            </DropdownMenuItem>
          ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export { NodeActionMenu };
