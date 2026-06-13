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
import { useUpdateCredentialFolderMutation } from "./hooks/useCredentialFolderMutations";
import type { CredentialFolder } from "./types/credentialFolderTypes";

interface EditCredentialFolderDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  folder: CredentialFolder;
}

function EditCredentialFolderDialog({
  open,
  onOpenChange,
  folder,
}: EditCredentialFolderDialogProps) {
  const [title, setTitle] = useState(folder.title);
  const [description, setDescription] = useState(folder.description || "");
  const updateFolderMutation = useUpdateCredentialFolderMutation();

  useEffect(() => {
    if (open) {
      setTitle(folder.title);
      setDescription(folder.description || "");
    }
  }, [open, folder]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!title.trim()) return;

    try {
      await updateFolderMutation.mutateAsync({
        folderId: folder.folder_id,
        data: {
          title: title.trim(),
          // Send the trimmed value (possibly "") rather than null so clearing the
          // field actually persists — the API treats null as "leave unchanged".
          description: description.trim(),
        },
      });
      onOpenChange(false);
    } catch {
      // onError toast already surfaces the failure
    }
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
              <Label htmlFor="edit-credential-folder-title">Title</Label>
              <Input
                id="edit-credential-folder-title"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder="e.g., Production Credentials"
                autoFocus
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="edit-credential-folder-description">
                Description (optional)
              </Label>
              <Textarea
                id="edit-credential-folder-description"
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

export { EditCredentialFolderDialog };
