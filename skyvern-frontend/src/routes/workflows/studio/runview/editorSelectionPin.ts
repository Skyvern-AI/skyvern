import {
  isBlockItem,
  type WorkflowRunTimelineItem,
} from "@/routes/workflows/types/workflowRunTypes";

type TimelineBlock = {
  blockId: string;
  label: string | null;
  actionIds: Array<string>;
};

function collectBlocks(
  items: ReadonlyArray<WorkflowRunTimelineItem>,
): Array<TimelineBlock> {
  const blocks: Array<TimelineBlock> = [];
  const walk = (list: ReadonlyArray<WorkflowRunTimelineItem>): void => {
    for (const item of list) {
      if (isBlockItem(item)) {
        blocks.push({
          blockId: item.block.workflow_run_block_id,
          label: item.block.label,
          actionIds: (item.block.actions ?? []).flatMap((action) =>
            action.action_id ? [action.action_id] : [],
          ),
        });
      }
      if (item.children.length > 0) {
        walk(item.children);
      }
    }
  };
  walk(items);
  return blocks;
}

/**
 * The run block to pin when the editor canvas selects a block, so the Run pane's
 * detail and the Browser pane's screenshot (which follows ?active=) both land on
 * it — the reverse of the timeline→canvas jump in resolveTimelineBlockJumpNodeId.
 * null means leave the pin alone.
 *
 * Held to a finalized, whole-workflow run: while one is still executing the run
 * surfaces are watching the live edge, and a block run (?bl=) keeps the live
 * debug browser as its surface even once finalized (the block-iterate loop, see
 * resolveBrowserPaneView) — pinning a frame would scrub the Browser pane off it.
 * Either way, merely opening a block's settings must not yank the panes.
 *
 * Held to a Run pane that is already open, too. RunView stays mounted while its
 * pane is closed, and ?active= — what this pin mirrors to — is itself a run
 * reference: writing one from an edit-class layout opens the run surfaces and
 * remaps the whole studio (layoutClassForSearch / panesFromDeepLink), so a click
 * meant to author a block drops the user into the last finished run instead.
 *
 * `systemFocusLabel` is the machine-origin marker (SYSTEM_BLOCK_FOCUS_PARAM):
 * the copilot's build-follow moves the canvas, which mirrors into
 * ?selected-block=, and pinning that would walk the Run and Browser panes
 * through an old finalized run while the build streams. Only a selection the
 * user made pins.
 */
export function resolveEditorSelectionPin({
  editorOpen,
  runPaneOpen,
  finalized,
  blockRun,
  timeline,
  selectedBlockLabel,
  systemFocusLabel,
  pinnedFrameId,
}: {
  editorOpen: boolean;
  runPaneOpen: boolean;
  finalized: boolean;
  blockRun: boolean;
  timeline: ReadonlyArray<WorkflowRunTimelineItem> | undefined;
  selectedBlockLabel: string | null;
  systemFocusLabel: string | null;
  pinnedFrameId: string | null;
}): string | null {
  if (
    !editorOpen ||
    !runPaneOpen ||
    !finalized ||
    blockRun ||
    !timeline ||
    !selectedBlockLabel ||
    selectedBlockLabel === systemFocusLabel
  ) {
    return null;
  }
  // Labels are only softly unique and a looped block executes many times, so a
  // label can name several run blocks.
  const matches = collectBlocks(timeline).filter(
    (block) => block.label === selectedBlockLabel,
  );
  if (matches.length === 0) {
    return null;
  }
  // Already somewhere inside this block — an action the user picked, or the
  // block itself. Re-pinning would drop them back to the block header and, on
  // the timeline→canvas→timeline path, bounce the selection between panes.
  if (
    pinnedFrameId !== null &&
    matches.some(
      (block) =>
        block.blockId === pinnedFrameId ||
        block.actionIds.includes(pinnedFrameId),
    )
  ) {
    return null;
  }
  // ponytail: walk order picks the occurrence for a looped block; iterations are
  // navigated from the timeline. Sort by start time if that proves wrong.
  return matches[0]!.blockId;
}
