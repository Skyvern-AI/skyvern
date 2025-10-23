import { useState } from "react";
import { CheckIcon, Cross2Icon, FileIcon, MagnifyingGlassIcon } from "@radix-ui/react-icons";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Input } from "@/components/ui/input";
import { cn } from "@/util/utils";
import { useFoldersQuery } from "../hooks/useFoldersQuery";
import { useUpdateWorkflowFolderMutation } from "../hooks/useFolderMutations";

interface WorkflowFolderSelectorProps {
  workflowId: string;
  currentFolderId: string | null;
}

function WorkflowFolderSelector({
  workflowId,
  currentFolderId,
}: WorkflowFolderSelectorProps) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const { data: folders = [] } = useFoldersQuery({ search, page_size: 100 });
  const updateFolderMutation = useUpdateWorkflowFolderMutation();

  const handleFolderSelect = async (folderId: string | null) => {
    await updateFolderMutation.mutateAsync({
      workflowId,
      data: { folder_id: folderId },
    });
    setOpen(false);
    setSearch("");
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className={cn(
            "h-8 w-8",
            currentFolderId ? "text-blue-400" : "text-slate-400"
          )}
          title="Move to folder"
        >
          <FileIcon className="h-4 w-4" />
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-80 p-0" align="end">
        <div className="border-b p-3">
          <h4 className="mb-2 text-sm font-medium">Move to folder</h4>
          <div className="relative">
            <MagnifyingGlassIcon className="absolute left-2 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
            <Input
              placeholder="Search folders..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="h-8 pl-8"
              autoFocus
            />
          </div>
        </div>
        <div className="max-h-[300px] overflow-y-auto">
          {currentFolderId && (
            <button
              onClick={() => handleFolderSelect(null)}
              className="flex w-full items-center justify-between border-b px-3 py-2 text-left text-sm transition-colors hover:bg-slate-50 dark:hover:bg-slate-800"
            >
              <div className="flex items-center gap-2">
                <Cross2Icon className="h-4 w-4 text-red-400" />
                <span>Remove from folder</span>
              </div>
            </button>
          )}

          {folders.length === 0 ? (
            <div className="px-3 py-8 text-center text-sm text-slate-400">
              No folders found
            </div>
          ) : (
            folders.map((folder) => {
              const isCurrentFolder = currentFolderId === folder.folder_id;
              return (
                <button
                  key={folder.folder_id}
                  onClick={() => handleFolderSelect(folder.folder_id)}
                  disabled={isCurrentFolder}
                  className="flex w-full items-center justify-between px-3 py-2 text-left text-sm transition-colors hover:bg-slate-50 disabled:opacity-50 dark:hover:bg-slate-800"
                >
                  <div className="flex items-center gap-2">
                    <FileIcon className="h-4 w-4 text-blue-400" />
                    <div className="flex flex-col">
                      <span>{folder.title}</span>
                      {folder.description && (
                        <span className="text-xs text-slate-400">
                          {folder.description}
                        </span>
                      )}
                    </div>
                  </div>
                  {isCurrentFolder && (
                    <CheckIcon className="h-4 w-4 text-blue-400" />
                  )}
                </button>
              );
            })
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}

export { WorkflowFolderSelector };

