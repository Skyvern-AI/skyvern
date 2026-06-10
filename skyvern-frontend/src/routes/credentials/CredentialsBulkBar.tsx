import { getClient } from "@/api/AxiosClient";
import { CredentialApiResponse } from "@/api/types";
import { FolderIcon } from "@/components/icons/FolderIcon";
import { GarbageIcon } from "@/components/icons/GarbageIcon";
import { SelectionBar, SelectionBarDivider } from "@/components/SelectionBar";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { bulkResultToast } from "@/util/bulkResultToast";
import {
  BULK_CONCURRENCY_LIMIT,
  runWithConcurrency,
} from "@/util/runWithConcurrency";
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { CredentialFolderSelector } from "./CredentialFolderSelector";

type Props = {
  selectedCredentials: CredentialApiResponse[];
  isOperating: boolean;
  onOperatingChange: (operating: boolean) => void;
  onClear: () => void;
  onReplaceSelection: (ids: Iterable<string>) => void;
};

function CredentialsBulkBar({
  selectedCredentials,
  isOperating,
  onOperatingChange,
  onClear,
  onReplaceSelection,
}: Props) {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const [deleteDialog, setDeleteDialog] = useState<{
    open: boolean;
    targets: string[];
  }>({ open: false, targets: [] });
  const count = selectedCredentials.length;

  async function handleBulkMoveToFolder(folderId: string | null) {
    onOperatingChange(true);
    try {
      const client = await getClient(credentialGetter);
      const results = await runWithConcurrency(
        selectedCredentials.map(
          (credential) => () =>
            client.put(`/credentials/${credential.credential_id}/folder`, {
              folder_id: folderId,
            }),
        ),
        BULK_CONCURRENCY_LIMIT,
      );
      const succeeded = results.filter((r) => r.status === "fulfilled").length;
      bulkResultToast({
        succeeded,
        total: count,
        results,
        successTitle: (n) =>
          folderId
            ? `Moved ${n} credential${n !== 1 ? "s" : ""} to folder.`
            : `Removed ${n} credential${n !== 1 ? "s" : ""} from folder.`,
        failureTitle: (n) =>
          folderId
            ? `Failed to move ${n} credential${n !== 1 ? "s" : ""} to folder.`
            : `Failed to remove ${n} credential${n !== 1 ? "s" : ""} from folder.`,
        partialTitle: (successCount, failedCount) =>
          folderId
            ? `Moved ${successCount} credential${successCount !== 1 ? "s" : ""} to folder. ${failedCount} failed.`
            : `Removed ${successCount} credential${successCount !== 1 ? "s" : ""} from folder. ${failedCount} failed.`,
      });
      if (succeeded === count) {
        onClear();
      }
      if (succeeded > 0) {
        queryClient.invalidateQueries({ queryKey: ["credentials"] });
        queryClient.invalidateQueries({ queryKey: ["credential-folders"] });
      }
    } finally {
      onOperatingChange(false);
    }
  }

  async function handleBulkDeleteConfirm() {
    const targets = deleteDialog.targets;
    if (targets.length === 0) {
      return;
    }
    onOperatingChange(true);
    try {
      const client = await getClient(credentialGetter);
      const results = await runWithConcurrency(
        targets.map(
          (credentialId) => () => client.delete(`/credentials/${credentialId}`),
        ),
        BULK_CONCURRENCY_LIMIT,
      );
      const failedIds = new Set<string>();
      results.forEach((result, index) => {
        if (result.status === "rejected") {
          failedIds.add(targets[index]!);
        }
      });
      const succeeded = targets.length - failedIds.size;
      bulkResultToast({
        succeeded,
        total: targets.length,
        results,
        successTitle: (n) => `Deleted ${n} credential${n !== 1 ? "s" : ""}.`,
        failureTitle: (n) =>
          `Failed to delete ${n} credential${n !== 1 ? "s" : ""}.`,
        partialTitle: (successCount, failedCount) =>
          `Deleted ${successCount} credential${successCount !== 1 ? "s" : ""}. ${failedCount} failed.`,
      });
      if (failedIds.size === 0) {
        onClear();
      } else {
        onReplaceSelection(failedIds);
      }
      if (succeeded > 0) {
        queryClient.invalidateQueries({ queryKey: ["credentials"] });
        queryClient.invalidateQueries({ queryKey: ["credential-folders"] });
      }
    } finally {
      onOperatingChange(false);
      setDeleteDialog({ open: false, targets: [] });
    }
  }

  return (
    <>
      <SelectionBar count={count} isOperating={isOperating} onClear={onClear}>
        <CredentialFolderSelector
          currentFolderId={null}
          bulkCount={count}
          onBulkFolderSelect={handleBulkMoveToFolder}
          bulkHasFolders={selectedCredentials.some(
            (credential) => (credential.folder_id ?? null) !== null,
          )}
          disabled={isOperating}
          trigger={
            <Button size="sm" variant="ghost" disabled={isOperating}>
              <FolderIcon className="mr-1.5 h-4 w-4" />
              Move to folder
            </Button>
          }
        />
        <SelectionBarDivider />
        <Button
          size="sm"
          variant="ghost"
          className="text-destructive hover:bg-destructive/10 hover:text-destructive"
          disabled={isOperating}
          onClick={() =>
            setDeleteDialog({
              open: true,
              targets: selectedCredentials.map(
                (credential) => credential.credential_id,
              ),
            })
          }
        >
          <GarbageIcon className="mr-1.5 h-4 w-4" />
          Delete
        </Button>
      </SelectionBar>
      <Dialog
        open={deleteDialog.open}
        onOpenChange={(open) => {
          if (!open && !isOperating) {
            setDeleteDialog({ open: false, targets: [] });
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              Delete {deleteDialog.targets.length} Credential
              {deleteDialog.targets.length === 1 ? "" : "s"}
            </DialogTitle>
            <DialogDescription>
              Are you sure you want to delete {deleteDialog.targets.length}{" "}
              {deleteDialog.targets.length === 1 ? "credential" : "credentials"}
              ? This action cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="secondary"
              disabled={isOperating}
              onClick={() => setDeleteDialog({ open: false, targets: [] })}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={isOperating}
              onClick={() => {
                void handleBulkDeleteConfirm();
              }}
            >
              {isOperating ? "Deleting..." : "Delete"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

export { CredentialsBulkBar };
