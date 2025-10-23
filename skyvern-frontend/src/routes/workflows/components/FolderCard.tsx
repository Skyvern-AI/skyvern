import { FileIcon } from "@radix-ui/react-icons";
import { cn } from "@/util/utils";
import type { Folder } from "../types/folderTypes";

interface FolderCardProps {
  folder: Folder;
  isSelected: boolean;
  onClick: () => void;
}

function FolderCard({ folder, isSelected, onClick }: FolderCardProps) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "flex h-24 flex-col gap-3 rounded-lg border p-4 text-left transition-colors hover:border-blue-400",
        isSelected
          ? "border-blue-400 bg-blue-50 ring-2 ring-blue-400/20 dark:bg-blue-950/20"
          : "border-slate-200 bg-slate-elevation1 dark:border-slate-700"
      )}
    >
      <div className="flex items-start gap-3">
        <div className="mt-0.5">
          <FileIcon className="h-5 w-5 text-blue-400" />
        </div>
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <h3 className="truncate text-sm font-medium text-slate-900 dark:text-slate-100">
            {folder.title}
          </h3>
          <p className="text-xs text-slate-500 dark:text-slate-400">
            {folder.workflow_count}{" "}
            {folder.workflow_count === 1 ? "workflow" : "workflows"}
          </p>
        </div>
      </div>
    </button>
  );
}

export { FolderCard };

