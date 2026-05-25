import { Pencil1Icon } from "@radix-ui/react-icons";
import { FolderIcon } from "@/components/icons/FolderIcon";
import { cn } from "@/util/utils";
import type { Folder } from "../types/folderTypes";
import { DeleteFolderButton } from "./DeleteFolderButton";
import { EditFolderDialog } from "./EditFolderDialog";
import { useState } from "react";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

interface FolderCardProps {
  folder: Folder;
  isSelected: boolean;
  onClick: () => void;
}

function FolderCard({ folder, isSelected, onClick }: FolderCardProps) {
  const [isHovered, setIsHovered] = useState(false);
  const [isEditDialogOpen, setIsEditDialogOpen] = useState(false);

  return (
    <>
      <button
        onClick={onClick}
        onMouseEnter={() => setIsHovered(true)}
        onMouseLeave={() => setIsHovered(false)}
        className={cn(
          "relative flex h-24 flex-col gap-3 rounded-lg border p-4 text-left transition-colors hover:border-blue-400",
          isSelected
            ? "border-blue-400 bg-blue-50 ring-2 ring-blue-400/20 dark:bg-blue-950/20"
            : "border-border bg-slate-elevation1 dark:border-border",
        )}
      >
        <div className="flex items-start gap-3">
          <div className="mt-0.5">
            <FolderIcon className="h-5 w-5 text-blue-400" />
          </div>
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <div className="flex items-start justify-between gap-2">
              <h3 className="truncate text-sm font-medium text-foreground dark:text-foreground">
                {folder.title}
              </h3>
              <div
                onClick={(e) => e.stopPropagation()}
                className={cn(
                  "flex gap-1 transition-opacity",
                  isHovered ? "opacity-100" : "opacity-0",
                )}
              >
                <TooltipProvider>
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <button
                        onClick={() => setIsEditDialogOpen(true)}
                        className="rounded p-1.5 text-muted-foreground transition-colors hover:bg-slate-elevation5/20 hover:text-muted-foreground"
                        aria-label="Edit folder"
                      >
                        <Pencil1Icon className="h-4 w-4" />
                      </button>
                    </TooltipTrigger>
                    <TooltipContent>Edit Folder</TooltipContent>
                  </Tooltip>
                </TooltipProvider>
                <DeleteFolderButton
                  folderId={folder.folder_id}
                  folderTitle={folder.title}
                />
              </div>
            </div>
            <p className="text-xs text-muted-foreground dark:text-muted-foreground">
              {folder.workflow_count}{" "}
              {folder.workflow_count === 1 ? "workflow" : "workflows"}
            </p>
          </div>
        </div>
      </button>
      <EditFolderDialog
        open={isEditDialogOpen}
        onOpenChange={setIsEditDialogOpen}
        folder={folder}
      />
    </>
  );
}

export { FolderCard };
