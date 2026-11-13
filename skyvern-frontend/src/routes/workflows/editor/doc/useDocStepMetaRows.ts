import { useContext } from "react";

import type { WorkflowBlockNode } from "../nodes";
import { useDocStepAdded, useDocStepChangedKeys } from "./DocProposalView";
import { docSettingRowsSplit, type DocSettingRow } from "./docSettingsChips";
import { WorkflowDocActionsContext } from "./WorkflowDocActionsContext";

/** The step's key settings on its summary line and in Details, decided as the editor's problem check decides them;
 * `proposed` says which keys a pending proposal sets. */
export function useDocStepMetaRows(
  node: WorkflowBlockNode,
  rows: readonly DocSettingRow[],
) {
  const actions = useContext(WorkflowDocActionsContext);
  const changed = useDocStepChangedKeys(node);
  const added = useDocStepAdded(node);
  const editable = actions.stepLinesEditable(node.id);
  return {
    // Only a key the proposal changes lifts the first-page rule: an added step that inherits the open page lists no
    // empty URL.
    ...docSettingRowsSplit(
      rows,
      editable,
      actions.opensFirstPage(node.id),
      (key) => changed.includes(key),
    ),
    proposed: (key: string) => added || changed.includes(key),
    editable,
  };
}
