import type { RunBlockingBlock } from "./getRunBlockingBlocks";

export function getRunBlockingTooltipText(
  blocks: Array<RunBlockingBlock>,
): string {
  if (blocks.length === 0) {
    return "Select credentials for login blocks before running.";
  }
  if (blocks.length === 1) {
    return `Select a credential for the login block "${blocks[0]?.label}" before running.`;
  }
  return `Select credentials for these login blocks before running: ${blocks.map((block) => block.label).join(", ")}.`;
}
