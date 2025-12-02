import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { useUpdateFolderMutation } from "../hooks/useFolderMutations";
import type { Folder } from "../types/folderTypes";

interface EditFolderDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  folder: Folder;
}

function EditFolderDialog({
  open,
  onOpenChange,
  folder,
}: EditFolderDialogProps) {
  const [title, setTitle] = useState(folder.title);
  const [description, setDescription] = useState(folder.description || "");
  const updateFolderMutation = useUpdateFolderMutation();

  // Reset form when folder changes or dialog opens
  useEffect(() => {
    if (open) {
      setTitle(folder.title);
      setDescription(folder.description || "");
    }
  }, [open, folder]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!title.trim()) return;

    await updateFolderMutation.mutateAsync({
      folderId: folder.folder_id,
      data: {
        title: title.trim(),
        description: description.trim() || null,
      },
    });

    onOpenChange(false);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Edit Folder</DialogTitle>
          <DialogDescription>
            Update the folder's title and description.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit}>
          <div className="grid gap-4 py-4">
            <div className="grid gap-2">
              <Label htmlFor="edit-folder-title">Title</Label>
              <Input
                id="edit-folder-title"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder="e.g., Production Workflows"
                autoFocus
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="edit-folder-description">
                Description (optional)
              </Label>
              <Textarea
                id="edit-folder-description"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="Add a description..."
                rows={3}
              />
            </div>
          </div>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={!title.trim() || updateFolderMutation.isPending}
            >
              Save Changes
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export { EditFolderDialog };
