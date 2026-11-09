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
  const [isEditDialogOpen, setIsEditDialogOpen] = useState(false);

  return (
    <>
      <div className="group relative">
        <button
          onClick={onClick}
          aria-pressed={isSelected}
          className={cn(
            "flex h-24 w-full flex-col gap-3 rounded-lg border p-4 text-left transition-colors group-hover:border-blue-400",
            isSelected
              ? "border-blue-400 bg-blue-50 ring-2 ring-blue-400/20 dark:bg-blue-950/20"
              : "border-slate-200 bg-slate-elevation1 dark:border-slate-700",
          )}
        >
          <div className="flex w-full items-start gap-3">
            <div className="mt-0.5">
              <FolderIcon className="h-5 w-5 text-blue-700 dark:text-blue-400" />
            </div>
            <div className="flex min-w-0 flex-1 flex-col gap-1">
              <h3
                title={folder.title}
                className="h-7 truncate text-sm font-medium text-slate-900 group-hover:pr-16 group-has-[:focus-visible]:pr-16 dark:text-slate-100 [@media(hover:none)]:pr-16"
              >
                {folder.title}
              </h3>
              <p className="text-xs text-muted-foreground dark:text-slate-400">
                {folder.workflow_count}{" "}
                {folder.workflow_count === 1 ? "agent" : "agents"}
              </p>
            </div>
          </div>
        </button>
        <div className="absolute right-4 top-4 flex gap-1 opacity-0 transition-opacity group-hover:opacity-100 group-has-[:focus-visible]:opacity-100 [@media(hover:none)]:opacity-100">
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <button
                  onClick={() => setIsEditDialogOpen(true)}
                  className="rounded p-1.5 text-muted-foreground transition-colors hover:bg-slate-500/20 hover:text-tertiary-foreground"
                  aria-label={`Edit folder ${folder.title}`}
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
      <EditFolderDialog
        open={isEditDialogOpen}
        onOpenChange={setIsEditDialogOpen}
        folder={folder}
      />
    </>
  );
}

export { FolderCard };
