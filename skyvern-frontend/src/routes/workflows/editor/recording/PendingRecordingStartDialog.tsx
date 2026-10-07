import { ReloadIcon } from "@radix-ui/react-icons";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { toast } from "@/components/ui/use-toast";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import {
  getWorkflowLockMessage,
  selectEditorMutationLocked,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";

import { usePendingRecordingStartGate } from "./pendingRecordingStartGate";

function startOrExplain(start: (() => boolean) | null) {
  if (start && !start())
    toast({
      title: "Recording didn't start",
      description: "Where you chose to record changed. Click Record again.",
    });
}

const clearedGate = { blockedStart: null, awaitingConfirmedSave: false };

export function PendingRecordingStartDialog({
  onSave,
  onDiscard,
}: {
  onSave: () => Promise<void>;
  onDiscard: () => void;
}) {
  const blockedStart = usePendingRecordingStartGate((s) => s.blockedStart);
  const discardRecords = usePendingRecordingStartGate((s) => s.discardRecords);
  const awaitingConfirmedSave = usePendingRecordingStartGate(
    (s) => s.awaitingConfirmedSave,
  );
  const pendingRecordingId = useWorkflowHasChangesStore(
    (s) => s.pendingRecordingId,
  );
  const saveBlockedReason = useWorkflowHasChangesStore(
    (s) => s.saveBlockedReason,
  );
  const showConfirmCodeCacheDeletion = useWorkflowHasChangesStore(
    (s) => s.showConfirmCodeCacheDeletion,
  );
  const editorLocked = useWorkflowYamlEditorStore(selectEditorMutationLocked);
  const authoringInProgress = useWorkflowYamlEditorStore(
    (s) => s.authoringInProgress,
  );
  const editorOwner = useWorkflowYamlEditorStore((s) => s.editorOwner);
  const [saving, setSaving] = useState(false);
  // Another save or a Copilot turn holds the editor: Discard would reload
  // under it and the start would then be refused.
  const lockedElsewhere = (editorLocked || authoringInProgress) && !saving;
  const locked = Boolean(saveBlockedReason) || lockedElsewhere;
  const saveRef = useRef<HTMLButtonElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (pendingRecordingId !== null) return;
    const gate = usePendingRecordingStartGate.getState();
    usePendingRecordingStartGate.setState(clearedGate);
    if (gate.awaitingConfirmedSave) startOrExplain(gate.blockedStart);
  }, [pendingRecordingId]);

  const commitInProgress = useWorkflowYamlEditorStore(
    (s) => s.commitInProgress,
  );
  // The confirmation closed and no save is running: it was cancelled or the
  // save failed. Closing it mid-save still lets that save start the recording.
  useEffect(() => {
    if (
      !showConfirmCodeCacheDeletion &&
      !commitInProgress &&
      usePendingRecordingStartGate.getState().awaitingConfirmedSave &&
      useWorkflowHasChangesStore.getState().pendingRecordingId !== null
    )
      usePendingRecordingStartGate.setState(clearedGate);
  }, [showConfirmCodeCacheDeletion, commitInProgress]);

  const dismiss = () => usePendingRecordingStartGate.setState(clearedGate);

  // Navigating to another workflow replaces the editor; its prompt goes too.
  useEffect(() => {
    const gate = usePendingRecordingStartGate.getState();
    if (gate.blockedStart && gate.owner !== editorOwner)
      usePendingRecordingStartGate.setState(clearedGate);
  }, [editorOwner]);

  // Starts only once the save actually resolved the pending change; a failed
  // or refused save keeps the changes and the current recording state.
  const save = async () => {
    const start = blockedStart;
    setSaving(true);
    try {
      await onSave();
    } catch {
      // The save path already reported the failure.
    } finally {
      setSaving(false);
    }
    const changes = useWorkflowHasChangesStore.getState();
    if (changes.pendingRecordingId === null) {
      dismiss();
      startOrExplain(start);
    } else if (changes.showConfirmCodeCacheDeletion) {
      usePendingRecordingStartGate.setState({ awaitingConfirmedSave: true });
    } else {
      dismiss();
    }
  };

  const discard = () => {
    const start = blockedStart;
    onDiscard();
    dismiss();
    if (useWorkflowHasChangesStore.getState().pendingRecordingId !== null) {
      toast({
        title: "Couldn't discard changes",
        description: "Your changes are still here. Reload the page to retry.",
        variant: "destructive",
      });
      return;
    }
    if (discardRecords) startOrExplain(start);
  };

  return (
    <Dialog
      open={blockedStart !== null && !awaitingConfirmedSave}
      onOpenChange={(open) => {
        if (!open && !saving) dismiss();
      }}
    >
      <DialogContent
        // Discard can't be undone, so it never takes the initial focus.
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          (locked ? cancelRef : saveRef).current?.focus();
        }}
      >
        <DialogHeader>
          <DialogTitle>Save or discard changes before recording</DialogTitle>
          <DialogDescription asChild>
            <div className="space-y-2">
              <p>
                {saveBlockedReason ||
                  (lockedElsewhere
                    ? editorLocked
                      ? `${getWorkflowLockMessage()}.`
                      : "Finish the current authoring action first."
                    : "Blocks from your last recording aren't saved yet.")}
              </p>
              <p>
                Discarding reloads the last saved version and removes these
                blocks and any other unsaved edits. This can't be undone.
              </p>
            </div>
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button
            ref={cancelRef}
            variant="secondary"
            disabled={saving}
            onClick={dismiss}
          >
            Cancel
          </Button>
          <Button
            variant="destructive"
            disabled={saving || locked}
            onClick={discard}
          >
            {discardRecords ? "Discard and record" : "Discard changes"}
          </Button>
          <Button ref={saveRef} disabled={saving || locked} onClick={save}>
            {saving && <ReloadIcon className="mr-2 size-4 animate-spin" />}
            Save and record
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
