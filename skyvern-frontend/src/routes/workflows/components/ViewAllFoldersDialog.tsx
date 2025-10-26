import { useState, useMemo } from "react";
import { MagnifyingGlassIcon, ReloadIcon } from "@radix-ui/react-icons";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { FolderCard } from "./FolderCard";
import { useInfiniteFoldersQuery } from "../hooks/useInfiniteFoldersQuery";
import { handleInfiniteScroll } from "@/util/utils";

interface ViewAllFoldersDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  selectedFolderId: string | null;
  onFolderSelect: (folderId: string | null) => void;
}

function ViewAllFoldersDialog({
  open,
  onOpenChange,
  selectedFolderId,
  onFolderSelect,
}: ViewAllFoldersDialogProps) {
  const [search, setSearch] = useState("");
  
  const {
    data,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
  } = useInfiniteFoldersQuery({
    search,
    page_size: 20, // Load 20 to ensure scrollbar appears
  });

  // Flatten pages into a single array
  const folders = useMemo(() => {
    return data?.pages.flatMap((page) => page) ?? [];
  }, [data]);

  const handleFolderClick = (folderId: string) => {
    onFolderSelect(selectedFolderId === folderId ? null : folderId);
    onOpenChange(false);
    setSearch("");
  };

  const handleOpenChange = (open: boolean) => {
    onOpenChange(open);
    if (!open) {
      setSearch("");
    }
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="max-w-4xl max-h-[90vh]">
        <DialogHeader>
          <DialogTitle>All Folders</DialogTitle>
          <DialogDescription>
            Browse and select from all folders. Scroll to load more.
          </DialogDescription>
        </DialogHeader>

        <div className="relative mb-4">
          <MagnifyingGlassIcon className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
          <Input
            type="text"
            placeholder="Search folders..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="pl-10"
          />
        </div>

        <div 
          className="max-h-[500px] overflow-y-auto pr-2 [&::-webkit-scrollbar]:w-2 [&::-webkit-scrollbar-track]:bg-slate-100 [&::-webkit-scrollbar-thumb]:bg-slate-300 [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:border-2 [&::-webkit-scrollbar-thumb]:border-slate-100 dark:[&::-webkit-scrollbar-track]:bg-slate-800 dark:[&::-webkit-scrollbar-thumb]:bg-slate-600 dark:[&::-webkit-scrollbar-thumb]:border-slate-800"
          onScroll={(e) => handleInfiniteScroll(e, fetchNextPage, hasNextPage, isFetchingNextPage)}
        >
          {folders.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-12 text-slate-400">
              <p>No folders found</p>
            </div>
          ) : (
            <>
              <div className="grid grid-cols-3 gap-4">
                {folders.map((folder) => (
                  <FolderCard
                    key={folder.folder_id}
                    folder={folder}
                    isSelected={selectedFolderId === folder.folder_id}
                    onClick={() => handleFolderClick(folder.folder_id)}
                  />
                ))}
              </div>
              {isFetchingNextPage && (
                <div className="mt-4 flex items-center justify-center py-4">
                  <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />
                  <span className="text-sm text-slate-400">
                    Loading more folders...
                  </span>
                </div>
              )}
              {!hasNextPage && folders.length > 20 && (
                <div className="mt-4 flex items-center justify-center py-4">
                  <span className="text-sm text-slate-400">
                    All folders loaded
                  </span>
                </div>
              )}
            </>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

export { ViewAllFoldersDialog };

