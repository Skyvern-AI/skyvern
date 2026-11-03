export type RunValidationBlock = {
  block_type: string;
  label: string;
  url?: string | null;
  parameters?: Array<unknown> | null;
  parameter_keys?: Array<unknown> | null;
  loop_blocks?: Array<RunValidationBlock> | null;
};

export type LoginBlockWithoutCredentials = { label: string };

// Backend preflight only considers a login unbound when it has no non-URL input.
function loginBlockHasBoundNonUrlParameter(block: RunValidationBlock): boolean {
  if (Array.isArray(block.parameters)) {
    return block.parameters.some((parameter) => {
      if (typeof parameter !== "object" || parameter === null) {
        return false;
      }
      const key = (parameter as Record<string, unknown>).key;
      return typeof key === "string" && key !== block.url;
    });
  }
  return (block.parameter_keys ?? []).some(
    (key) => typeof key === "string" && key !== block.url && key.length > 0,
  );
}

export function isLoginBlockMissingCredentials(
  block: RunValidationBlock,
): boolean {
  return (
    block.block_type === "login" && !loginBlockHasBoundNonUrlParameter(block)
  );
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
