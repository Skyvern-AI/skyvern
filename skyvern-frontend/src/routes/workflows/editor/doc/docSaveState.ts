import { AxiosError } from "axios";

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
): void {
  if (!(error instanceof AxiosError) || error.response || !owner) return;
  const pending =
    useWorkflowYamlEditorStore.getState().pendingSaves[
      owner.workflowPermanentId
    ];
  if (pending?.owner !== owner || !pending.persisting) return;
  markWorkflowSaveSettled(owner);
  finishSaveTransaction(owner);
}
