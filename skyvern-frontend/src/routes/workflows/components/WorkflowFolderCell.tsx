import * as React from "react";
import { ChevronDownIcon } from "@radix-ui/react-icons";
import { FolderIcon } from "@/components/icons/FolderIcon";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { cn } from "@/util/utils";
import { useMoveWorkflowToFolder } from "../hooks/useMoveWorkflowToFolder";
import { FolderPickerCommand } from "./FolderPickerCommand";
import { HighlightText } from "./HighlightText";

type Props = {
  workflowPermanentId: string;
  folderId: string | null;
  folderTitle: string | null;
  highlightQuery: string;
  disabled?: boolean;
};

// Inline folder picker for the agents table's Folder column, so assigning a
// folder doesn't require going through the row's actions menu.
function WorkflowFolderCell({
  workflowPermanentId,
  folderId,
  folderTitle,
  highlightQuery,
  disabled,
}: Props) {
  const [open, setOpen] = React.useState(false);
  const { moveToFolder, isMoving } =
    useMoveWorkflowToFolder(workflowPermanentId);
  const label = folderId ? folderTitle || folderId : null;

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          disabled={disabled || isMoving}
          aria-label={label ? `Folder: ${label}. Change folder` : "Set folder"}
          title={label ?? "Set folder"}
          className={cn(
            "group/folder -mx-2 flex w-[calc(100%+1rem)] min-w-0 items-center gap-1.5 rounded-md px-2 py-1 text-left text-sm transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50",
            open && "bg-accent",
          )}
        >
          {label ? (
            <>
              <FolderIcon className="h-3.5 w-3.5 shrink-0 text-blue-700 dark:text-blue-400" />
              <span className="truncate">
                <HighlightText text={label} query={highlightQuery} />
              </span>
            </>
          ) : (
            <span className="text-muted-foreground">-</span>
          )}
          <ChevronDownIcon
            className={cn(
              "ml-auto h-3.5 w-3.5 shrink-0 text-muted-foreground opacity-0 transition-opacity group-hover/folder:opacity-100 group-hover/row:opacity-100 group-focus-visible/folder:opacity-100",
              open && "opacity-100",
            )}
          />
        </button>
      </PopoverTrigger>
      <PopoverContent
        className="w-72 p-0"
        align="start"
        // Portal events still bubble through the React tree; keep them from
        // reaching row-level handlers.
        onClick={(event) => event.stopPropagation()}
        onContextMenu={(event) => event.stopPropagation()}
      >
        <FolderPickerCommand
          currentFolderId={folderId}
          disabled={isMoving}
          onSelect={(nextFolderId) => {
            setOpen(false);
            void moveToFolder(nextFolderId);
          }}
        />
      </PopoverContent>
    </Popover>
  );
}

export { WorkflowFolderCell };
