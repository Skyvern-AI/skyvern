import { Pencil1Icon } from "@radix-ui/react-icons";
import { FolderIcon } from "@/components/icons/FolderIcon";
import { cn } from "@/util/utils";
import type { CredentialFolder } from "./types/credentialFolderTypes";
import { DeleteCredentialFolderButton } from "./DeleteCredentialFolderButton";
import { EditCredentialFolderDialog } from "./EditCredentialFolderDialog";
import { useState } from "react";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

interface CredentialFolderCardProps {
  folder: CredentialFolder;
  isSelected: boolean;
  onClick: () => void;
}

function CredentialFolderCard({
  folder,
  isSelected,
  onClick,
}: CredentialFolderCardProps) {
  const [isEditDialogOpen, setIsEditDialogOpen] = useState(false);

  return (
    <>
      <div
        role="button"
        tabIndex={0}
        onClick={onClick}
        onKeyDown={(e) => {
          // Only the card itself activates; let keydowns from the nested
          // Edit/Delete buttons through without also selecting the folder.
          if (
            e.target === e.currentTarget &&
            (e.key === "Enter" || e.key === " ")
          ) {
            e.preventDefault();
            onClick();
          }
        }}
        className={cn(
          "group relative flex h-24 cursor-pointer flex-col gap-3 rounded-lg border p-4 text-left transition-colors hover:border-blue-400",
          isSelected
            ? "border-blue-400 bg-blue-50 ring-2 ring-blue-400/20 dark:bg-blue-950/20"
            : "border-slate-200 bg-slate-elevation1 dark:border-slate-700",
        )}
      >
        <div className="flex items-start gap-3">
          <div className="mt-0.5">
            <FolderIcon className="h-5 w-5 text-blue-400" />
          </div>
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <div className="flex items-start justify-between gap-2">
              <h3 className="truncate text-sm font-medium text-slate-900 dark:text-slate-100">
                {folder.title}
              </h3>
              <div
                onClick={(e) => e.stopPropagation()}
                className="flex gap-1 opacity-0 transition-opacity focus-within:opacity-100 group-hover:opacity-100 [@media(hover:none)]:opacity-100"
              >
                <TooltipProvider>
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <button
                        onClick={() => setIsEditDialogOpen(true)}
                        className="rounded p-1.5 text-slate-400 transition-colors hover:bg-slate-500/20 hover:text-slate-300"
                        aria-label="Edit folder"
                      >
                        <Pencil1Icon className="h-4 w-4" />
                      </button>
                    </TooltipTrigger>
                    <TooltipContent>Edit Folder</TooltipContent>
                  </Tooltip>
                </TooltipProvider>
                <DeleteCredentialFolderButton
                  folderId={folder.folder_id}
                  folderTitle={folder.title}
                />
              </div>
            </div>
            <p className="text-xs text-slate-500 dark:text-slate-400">
              {folder.credential_count}{" "}
              {folder.credential_count === 1 ? "credential" : "credentials"}
            </p>
          </div>
        </div>
      </div>
      <EditCredentialFolderDialog
        open={isEditDialogOpen}
        onOpenChange={setIsEditDialogOpen}
        folder={folder}
      />
    </>
  );
}

export { CredentialFolderCard };
