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
import { useCreateFolderMutation } from "../hooks/useFolderMutations";

interface CreateFolderDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

function CreateFolderDialog({ open, onOpenChange }: CreateFolderDialogProps) {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const createFolderMutation = useCreateFolderMutation();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!title.trim()) return;

    await createFolderMutation.mutateAsync({
      title: title.trim(),
      description: description.trim() || null,
    });

    setTitle("");
    setDescription("");
    onOpenChange(false);
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
            Create a folder to organize your workflows.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit}>
          <div className="grid gap-4 py-4">
            <div className="grid gap-2">
              <Label htmlFor="folder-title">Title</Label>
              <Input
                id="folder-title"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder="e.g., Production Workflows"
                autoFocus
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="folder-description">Description (optional)</Label>
              <Textarea
                id="folder-description"
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

export { CreateFolderDialog };
