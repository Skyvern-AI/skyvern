import { AxiosError } from "axios";
import { create } from "zustand";

import { SaveRefusedError } from "@/store/WorkflowHasChangesStore";
import {
  type YamlCommitOwner,
  markWorkflowSaveSettled,
  finishSaveTransaction,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";

/** Plain-words reason a Save failed, or null when another notice already covers it (a newer version exists). */
export function describeSaveFailure(error: unknown): string | null {
  if (error instanceof AxiosError) {
    const status = error.response?.status;
    if (status === undefined) return "can't reach the server";
    if (status === 409) return null;
    const detail = (error.response?.data as { detail?: unknown } | undefined)
      ?.detail;
    if (typeof detail === "string" && detail) return detail;
    if (status >= 500) return "the server had a problem";
    return `the server refused it (${status})`;
  }
  if (error instanceof Error && error.message) return error.message;
  return "something went wrong";
}

/**
 * A save whose request ended with no answer keeps the editor locked as "still saving" in case the server wrote it.
 * Once the user has been told it failed, release that lock so Try again can run; the save's version precondition
 * turns a duplicate into a conflict instead of an overwrite.
 */
export function releaseUnansweredSave(
  error: unknown,
  owner: YamlCommitOwner | null,
): boolean {
  if (!(error instanceof AxiosError) || error.response || !owner) return false;
  const pending =
    useWorkflowYamlEditorStore.getState().pendingSaves[
      owner.workflowPermanentId
    ];
  if (pending?.owner !== owner || !pending.persisting) return false;
  markWorkflowSaveSettled(owner);
  finishSaveTransaction(owner);
  return true;
}

/**
 * Why the last Doc save of each workflow failed, shown on that workflow's edit bar with Try again until the next Doc
 * save or Run starts, or the Doc has no unsaved changes.
 */
export const useDocSaveFailureStore = create<{
  failures: Record<string, string>;
  setFailure: (workflowPermanentId: string, failure: string | null) => void;
}>((set) => ({
  failures: {},
  setFailure: (workflowPermanentId, failure) =>
    set(({ failures }) => {
      const next = { ...failures };
      if (failure) next[workflowPermanentId] = failure;
      else delete next[workflowPermanentId];
      return { failures: next };
    }),
}));

export type DocSaveOutcome = "saved" | "failed" | "released";

/**
 * Runs one Doc save for the editor on screen now. On failure it releases an unanswered save's lock and records the
 * reason for that workflow's edit bar; `released` means the lock was released, the one failure no other notice reports.
 */
export async function saveAndReport(
  save: () => Promise<unknown>,
  { recordRefusals = true }: { recordRefusals?: boolean } = {},
): Promise<DocSaveOutcome> {
  // The save that fails is the one started here, even if the editor has moved on to another workflow by then.
  const owner = useWorkflowYamlEditorStore.getState().editorOwner;
  const { setFailure } = useDocSaveFailureStore.getState();
  if (owner) setFailure(owner.workflowPermanentId, null);
  try {
    await save();
    return "saved";
  } catch (error) {
    const released = releaseUnansweredSave(error, owner);
    if (owner && (recordRefusals || !(error instanceof SaveRefusedError)))
      setFailure(owner.workflowPermanentId, describeSaveFailure(error));
    return released ? "released" : "failed";
  }
}
