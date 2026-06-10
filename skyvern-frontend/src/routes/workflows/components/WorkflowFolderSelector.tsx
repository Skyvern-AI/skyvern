import { useMemo, useState } from "react";
import {
  CheckIcon,
  Cross2Icon,
  MagnifyingGlassIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { FolderIcon } from "@/components/icons/FolderIcon";
import { Button } from "@/components/ui/button";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { Input } from "@/components/ui/input";
import { cn, handleInfiniteScroll } from "@/util/utils";
import { useInfiniteFoldersQuery } from "../hooks/useInfiniteFoldersQuery";
import { useUpdateWorkflowFolderMutation } from "../hooks/useFolderMutations";
import { useDebounce } from "use-debounce";
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/components/ui/use-toast";

interface WorkflowFolderSelectorProps {
  workflowPermanentId?: string;
  currentFolderId: string | null;
  bulkCount?: number;
  onBulkFolderSelect?: (folderId: string | null) => Promise<void>;
  bulkHasFolders?: boolean;
  disabled?: boolean;
  trigger?: React.ReactNode;
}

function WorkflowFolderSelector({
  workflowPermanentId,
  currentFolderId,
  bulkCount,
  onBulkFolderSelect,
  bulkHasFolders = true,
  disabled = false,
  trigger,
}: WorkflowFolderSelectorProps) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [debouncedSearch] = useDebounce(search, 500);
  const isTyping = search !== debouncedSearch;
  const isBulkMode = (bulkCount ?? 0) > 0 && Boolean(onBulkFolderSelect);

  const { data, fetchNextPage, hasNextPage, isFetchingNextPage, isFetching } =
    useInfiniteFoldersQuery({
      search: debouncedSearch,
      page_size: 20,
    });

  const folders = useMemo(() => {
    return data?.pages.flatMap((page) => page) ?? [];
  }, [data]);

  const updateFolderMutation = useUpdateWorkflowFolderMutation();

  async function handleFolderSelect(folderId: string | null) {
    // Close before the request; progress feedback lives in the caller (toast
    // or the bulk bar's Processing label), not in a hanging popover.
    setOpen(false);
    setSearch("");
    if (isBulkMode && onBulkFolderSelect) {
      await onBulkFolderSelect(folderId);
    } else if (workflowPermanentId) {
      try {
        await updateFolderMutation.mutateAsync({
          workflowPermanentId,
          data: { folder_id: folderId },
        });
      } catch (error) {
        toast({
          variant: "destructive",
          title: folderId
            ? "Failed to move agent to folder"
            : "Failed to remove agent from folder",
          description: error instanceof Error ? error.message : undefined,
        });
      }
    } else if (import.meta.env.DEV) {
      console.warn(
        "WorkflowFolderSelector needs workflowPermanentId (single mode) or onBulkFolderSelect (bulk mode).",
      );
    }
  }

  const showRemoveFromFolder = isBulkMode
    ? bulkHasFolders
    : Boolean(currentFolderId);

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        {trigger ?? (
          <Button
            variant="ghost"
            size="icon"
            disabled={disabled}
            className={cn(
              "text-muted-foreground hover:text-foreground",
              currentFolderId && "text-blue-400 hover:text-blue-400",
            )}
          >
            <FolderIcon className="h-4 w-4" />
          </Button>
        )}
      </PopoverTrigger>
      <PopoverContent
        className="w-80 p-0"
        align="end"
        onCloseAutoFocus={(e) => e.preventDefault()}
      >
        <div className="border-b p-3">
          <h4 className="mb-2 text-sm font-medium">
            {isBulkMode
              ? `Move ${bulkCount} agent${bulkCount === 1 ? "" : "s"} to folder`
              : "Move to folder"}
          </h4>
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
        <div
          className="max-h-[300px] overflow-y-auto overflow-x-hidden [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:border-2 [&::-webkit-scrollbar-thumb]:border-slate-100 [&::-webkit-scrollbar-thumb]:bg-slate-300 dark:[&::-webkit-scrollbar-thumb]:border-slate-800 dark:[&::-webkit-scrollbar-thumb]:bg-slate-600 [&::-webkit-scrollbar-track]:bg-slate-100 dark:[&::-webkit-scrollbar-track]:bg-slate-800 [&::-webkit-scrollbar]:w-2"
          onScroll={(e) =>
            handleInfiniteScroll(
              e,
              fetchNextPage,
              hasNextPage,
              isFetchingNextPage,
            )
          }
        >
          {showRemoveFromFolder && (
            <button
              type="button"
              onClick={() => {
                void handleFolderSelect(null);
              }}
              disabled={disabled || updateFolderMutation.isPending}
              className="flex w-full items-center justify-between border-b px-3 py-2 text-left text-sm transition-colors hover:bg-slate-50 disabled:opacity-50 dark:hover:bg-slate-800"
            >
              <div className="flex items-center gap-2">
                <Cross2Icon className="h-4 w-4 text-red-400" />
                <span>Remove from folder</span>
              </div>
            </button>
          )}

          {(isFetching || isTyping) && folders.length === 0 ? (
            <>
              {Array.from({ length: 8 }).map((_, index) => (
                <div
                  key={`skeleton-${index}`}
                  className="flex w-full items-center gap-2 px-3 py-2"
                >
                  <Skeleton className="h-4 w-4" />
                  <div className="flex flex-1 flex-col gap-1">
                    <Skeleton className="h-4 w-3/4" />
                    <Skeleton className="h-3 w-1/2" />
                  </div>
                </div>
              ))}
            </>
          ) : folders.length === 0 ? (
            <div className="px-3 py-8 text-center text-sm text-slate-400">
              No folders found
            </div>
          ) : (
            <>
              {folders.map((folder) => {
                const isCurrentFolder =
                  !isBulkMode && currentFolderId === folder.folder_id;
                return (
                  <button
                    key={folder.folder_id}
                    type="button"
                    onClick={() => {
                      void handleFolderSelect(folder.folder_id);
                    }}
                    disabled={
                      disabled ||
                      updateFolderMutation.isPending ||
                      isCurrentFolder
                    }
                    className="flex w-full items-center justify-between px-3 py-2 text-left text-sm transition-colors hover:bg-slate-50 disabled:opacity-50 dark:hover:bg-slate-800"
                  >
                    <div className="flex min-w-0 items-center gap-2">
                      <FolderIcon className="h-4 w-4 shrink-0 text-blue-400" />
                      <div className="flex min-w-0 flex-col">
                        <span className="break-words">{folder.title}</span>
                        {folder.description && (
                          <span className="line-clamp-2 break-words text-xs text-slate-400 [overflow-wrap:anywhere]">
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
              })}
              {isFetchingNextPage && (
                <div className="flex items-center justify-center py-2">
                  <ReloadIcon className="h-3 w-3 animate-spin text-slate-400" />
                </div>
              )}
            </>
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}

export { WorkflowFolderSelector };
