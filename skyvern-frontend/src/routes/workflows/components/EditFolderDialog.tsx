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
          <DialogTitle>폴더 편집</DialogTitle>
          <DialogDescription>
            폴더의 이름과 설명을 수정하세요.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit}>
          <div className="grid gap-4 py-4">
            <div className="grid gap-2">
              <Label htmlFor="edit-folder-title">이름</Label>
              <Input
                id="edit-folder-title"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder="예: 프로덕션 워크플로우"
                autoFocus
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="edit-folder-description">
                설명 (선택사항)
              </Label>
              <Textarea
                id="edit-folder-description"
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
              onClick={() => onOpenChange(false)}
            >
              취소
            </Button>
            <Button
              type="submit"
              disabled={!title.trim() || updateFolderMutation.isPending}
            >
              변경사항 저장
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export { EditFolderDialog };
