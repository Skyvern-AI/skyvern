import { useState } from "react";
import { MagnifyingGlassIcon } from "@radix-ui/react-icons";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import { FolderCard } from "./FolderCard";
import { useFoldersQuery } from "../hooks/useFoldersQuery";

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
  const { data: folders = [] } = useFoldersQuery({
    search,
    page_size: 10,
  });

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
      <DialogContent className="max-h-[80vh] max-w-4xl">
        <DialogHeader>
          <DialogTitle>All Folders</DialogTitle>
          <DialogDescription>
            Browse and select from all {folders.length} folders.
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

        <ScrollArea className="h-[500px] pr-4">
          {folders.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-12 text-slate-400">
              <p>No folders found</p>
            </div>
          ) : (
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
          )}
        </ScrollArea>
      </DialogContent>
    </Dialog>
  );
}

export { ViewAllFoldersDialog };

