import {
  getReadableActionType,
  Status,
  type ActionsApiResponse,
} from "@/api/types";
import { isActionSuccess } from "@/routes/workflows/components/actionStatus";
import {
  getActionSummary,
  normalizeInlineText,
} from "../../workflowBlockUtils";

export type DocStepAction = {
  key: string;
  type: string;
  text: string | null;
  failed: boolean;
};

export const MAX_SHOWN_ACTIONS = 10;

/** A run block's first actions as numbered lines (the action's kind and what it set out to do, else what came of it), and how many more there are. */
export function docStepActions(
  actions: ActionsApiResponse[] | null | undefined,
): { shown: DocStepAction[]; more: number } {
  // The timeline lists a block's actions newest first; the list reads in the order they ran.
  const ordered = [...(actions ?? [])].reverse();
  const shown = ordered.slice(0, MAX_SHOWN_ACTIONS).map((action, index) => {
    const summary = getActionSummary(action);
    return {
      key: `${action.action_id}-${index}`,
      type: getReadableActionType(action.action_type, {
        nullActionLabel: "Look at the page",
      }),
      text:
        normalizeInlineText(summary?.body?.text) ??
        normalizeInlineText(summary?.outcome),
      // A finished wait is stored as failed; the shared rule reads it as a success.
      failed: action.status === Status.Failed && !isActionSuccess(action),
    };
  });
  return { shown, more: ordered.length - shown.length };
}
