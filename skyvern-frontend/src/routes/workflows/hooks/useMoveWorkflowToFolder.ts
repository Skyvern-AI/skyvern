import { toast } from "@/components/ui/use-toast";
import { useUpdateWorkflowFolderMutation } from "./useFolderMutations";

function useMoveWorkflowToFolder(workflowPermanentId: string) {
  const updateFolderMutation = useUpdateWorkflowFolderMutation();

  async function moveToFolder(folderId: string | null) {
    try {
      await updateFolderMutation.mutateAsync({
        workflowPermanentId,
        data: { folder_id: folderId },
      });
      toast({
        title: folderId ? "Moved to folder." : "Removed from folder.",
        variant: "success",
      });
    } catch (error) {
      toast({
        variant: "destructive",
        title: folderId
          ? "Failed to move agent to folder"
          : "Failed to remove agent from folder",
        description: error instanceof Error ? error.message : undefined,
      });
    }
  }

  return { moveToFolder, isMoving: updateFolderMutation.isPending };
}

export { useMoveWorkflowToFolder };
