export type RunValidationBlock = {
  block_type: string;
  label: string;
  parameters?: Array<unknown> | null;
  parameter_keys?: Array<unknown> | null;
  loop_blocks?: Array<RunValidationBlock> | null;
};

export type LoginBlockWithoutCredentials = { label: string };

function loginBlockHasCredential(block: RunValidationBlock): boolean {
  if ("parameters" in block) {
    return (block.parameters?.length ?? 0) > 0;
  }
  return (block.parameter_keys?.length ?? 0) > 0;
}

export function isLoginBlockMissingCredentials(
  block: RunValidationBlock,
): boolean {
  return block.block_type === "login" && !loginBlockHasCredential(block);
}

function isNestedLoopRunValidationBlock(block: RunValidationBlock): boolean {
  return (
    (block.block_type === "for_loop" || block.block_type === "while_loop") &&
    Array.isArray(block.loop_blocks)
  );
}

export function getLoginBlocksWithoutCredentials(
  blocks: Array<RunValidationBlock>,
): Array<LoginBlockWithoutCredentials> {
  const result: Array<LoginBlockWithoutCredentials> = [];

  for (const block of blocks) {
    if (isLoginBlockMissingCredentials(block)) {
      result.push({ label: block.label });
    }

    if (isNestedLoopRunValidationBlock(block)) {
      result.push(...getLoginBlocksWithoutCredentials(block.loop_blocks ?? []));
    }
  }

  return result;
}
