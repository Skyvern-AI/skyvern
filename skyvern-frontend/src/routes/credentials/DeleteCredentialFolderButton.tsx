import { TrashSolidIcon } from "@/components/icons/TrashSolidIcon";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
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
    <>
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              onClick={(e) => {
                e.stopPropagation();
                setIsDialogOpen(true);
              }}
              className="rounded p-1.5 text-red-400 transition-colors hover:bg-red-500/20 hover:text-red-300"
              aria-label="Delete folder"
            >
              <TrashSolidIcon className="h-4 w-4" />
            </button>
          </TooltipTrigger>
          <TooltipContent>Delete Folder</TooltipContent>
        </Tooltip>
      </TooltipProvider>
      <ConfirmDialog
        open={isDialogOpen}
        onOpenChange={(open) => {
          if (!open) {
            setIsDialogOpen(false);
          }
        }}
        title={`Delete folder: ${folderTitle}?`}
        description={
          <p>The credentials in this folder will be unassigned, not deleted.</p>
        }
        reversible
        isPending={isDeleteFolderPending}
        onConfirm={() => deleteFolder({ folderId })}
      />
    </>
  );
}

export { DeleteCredentialFolderButton };
