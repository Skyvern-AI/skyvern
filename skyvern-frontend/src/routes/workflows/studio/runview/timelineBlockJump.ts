import { type BlockSearchTarget } from "@/routes/workflows/studio/blockSearch";

// null = no jump: editor pane closed (selection must not move a hidden canvas),
// unlabeled block, or a label that isn't a single match in the current draft —
// absent (edited since the run) or, since labels are only softly unique,
// ambiguous, where a jump can't tell which node was meant.
export function resolveTimelineBlockJumpNodeId({
  editorOpen,
  targets,
  label,
}: {
  editorOpen: boolean;
  targets: Array<BlockSearchTarget>;
  label: string | null;
}): string | null {
  if (!editorOpen || !label) {
    return null;
  }
  const [match, ...rest] = targets.filter((target) => target.label === label);
  return match !== undefined && rest.length === 0 ? match.nodeId : null;
}
