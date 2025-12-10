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
          <DialogTitle>새 폴더 만들기</DialogTitle>
          <DialogDescription>
            워크플로우를 정리할 폴더를 만드세요.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit}>
          <div className="grid gap-4 py-4">
            <div className="grid gap-2">
              <Label htmlFor="folder-title">이름</Label>
              <Input
                id="folder-title"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder="예: 프로덕션 워크플로우"
                autoFocus
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="folder-description">설명 (선택사항)</Label>
              <Textarea
                id="folder-description"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="설명을 입력하세요..."
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
              취소
            </Button>
            <Button
              type="submit"
              disabled={!title.trim() || createFolderMutation.isPending}
            >
              폴더 만들기
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export { CreateFolderDialog };
