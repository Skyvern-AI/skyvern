import { useState } from "react";
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
import { useCreateCredentialFolderMutation } from "./hooks/useCredentialFolderMutations";

interface CreateCredentialFolderDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

function CreateCredentialFolderDialog({
  open,
  onOpenChange,
}: CreateCredentialFolderDialogProps) {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const createFolderMutation = useCreateCredentialFolderMutation();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!title.trim()) return;

    try {
      await createFolderMutation.mutateAsync({
        title: title.trim(),
        description: description.trim() || null,
      });
      setTitle("");
      setDescription("");
      onOpenChange(false);
    } catch {
      // onError toast already surfaces the failure
    }
  };

  const handleOpenChange = (open: boolean) => {
    onOpenChange(open);
    if (!open) {
      setTitle("");
      setDescription("");
    }
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Create New Folder</DialogTitle>
          <DialogDescription>
            Create a folder to organize your credentials.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit}>
          <div className="grid gap-4 py-4">
            <div className="grid gap-2">
              <Label htmlFor="credential-folder-title">Title</Label>
              <Input
                id="credential-folder-title"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder="e.g., Production Credentials"
                autoFocus
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="credential-folder-description">
                Description (optional)
              </Label>
              <Textarea
                id="credential-folder-description"
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
              onClick={() => handleOpenChange(false)}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={!title.trim() || createFolderMutation.isPending}
            >
              Create Folder
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export { CreateCredentialFolderDialog };
