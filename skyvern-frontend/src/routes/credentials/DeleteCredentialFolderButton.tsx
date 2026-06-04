import { GarbageIcon } from "@/components/icons/GarbageIcon";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { ReloadIcon } from "@radix-ui/react-icons";
import { useEffect, useState } from "react";
import { useDeleteCredentialFolderMutation } from "./hooks/useCredentialFolderMutations";

type Props = {
  folderId: string;
  folderTitle: string;
};

function DeleteCredentialFolderButton({ folderId, folderTitle }: Props) {
  const [isDialogOpen, setIsDialogOpen] = useState(false);
  const {
    mutate: deleteFolder,
    isPending: isDeleteFolderPending,
    isSuccess: isDeleteFolderSuccess,
  } = useDeleteCredentialFolderMutation();

  useEffect(() => {
    if (isDeleteFolderSuccess) setIsDialogOpen(false);
  }, [isDeleteFolderSuccess]);

  return (
    <Dialog open={isDialogOpen} onOpenChange={setIsDialogOpen}>
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <DialogTrigger asChild>
              <button
                onClick={(e) => e.stopPropagation()}
                className="rounded p-1.5 text-red-400 transition-colors hover:bg-red-500/20 hover:text-red-300"
                aria-label="Delete folder"
              >
                <GarbageIcon className="h-4 w-4" />
              </button>
            </DialogTrigger>
          </TooltipTrigger>
          <TooltipContent>Delete Folder</TooltipContent>
        </Tooltip>
      </TooltipProvider>
      <DialogContent onCloseAutoFocus={(e) => e.preventDefault()}>
        <DialogHeader>
          <DialogTitle>Delete Folder: {folderTitle}</DialogTitle>
          <DialogDescription>
            The credentials in this folder will be unassigned, not deleted.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="secondary">Cancel</Button>
          </DialogClose>
          <Button
            variant="destructive"
            onClick={() => deleteFolder({ folderId })}
            disabled={isDeleteFolderPending}
          >
            {isDeleteFolderPending && (
              <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />
            )}
            Delete
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export { DeleteCredentialFolderButton };
