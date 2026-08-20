import { GarbageIcon } from "@/components/icons/GarbageIcon";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { useEffect, useState } from "react";
import { useDeleteFolderMutation } from "../hooks/useFolderMutations";

type Props = {
  folderId: string;
  folderTitle: string;
};

function DeleteFolderButton({ folderId, folderTitle }: Props) {
  const [deleteOption, setDeleteOption] = useState<
    "folder_only" | "folder_and_workflows"
  >("folder_only");
  const [isDialogOpen, setIsDialogOpen] = useState(false);
  const {
    mutate: deleteFolder,
    isPending: isDeleteFolderPending,
    isSuccess: isDeleteFolderSuccess,
  } = useDeleteFolderMutation();

  // Close dialog when deletion succeeds
  useEffect(() => {
    if (isDeleteFolderSuccess) setIsDialogOpen(false);
  }, [isDeleteFolderSuccess]);

  const handleDelete = () => {
    const deleteWorkflows = deleteOption === "folder_and_workflows";
    deleteFolder({ folderId, folderTitle, deleteWorkflows });
  };

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
              className="rounded p-1.5 text-red-700 transition-colors hover:bg-red-500/20 hover:text-red-700 dark:text-red-400 dark:hover:text-red-300"
              aria-label="Delete folder"
            >
              <GarbageIcon className="h-4 w-4" />
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
        description={<p>Choose how you want to delete this folder.</p>}
        reversible={deleteOption === "folder_only"}
        reversibilityNote="The agents in this folder will be permanently deleted. This can't be undone."
        isPending={isDeleteFolderPending}
        onConfirm={handleDelete}
      >
        <RadioGroup
          value={deleteOption}
          onValueChange={(value) =>
            setDeleteOption(value as typeof deleteOption)
          }
        >
          <div className="flex items-center space-x-2">
            <RadioGroupItem value="folder_only" id="folder_only" />
            <Label htmlFor="folder_only" className="font-normal">
              Delete folder only (agents will be unassigned)
            </Label>
          </div>
          <div className="flex items-center space-x-2">
            <RadioGroupItem
              value="folder_and_workflows"
              id="folder_and_workflows"
            />
            <Label htmlFor="folder_and_workflows" className="font-normal">
              Delete folder and all agents inside it
            </Label>
          </div>
        </RadioGroup>
      </ConfirmDialog>
    </>
  );
}

export { DeleteFolderButton };
