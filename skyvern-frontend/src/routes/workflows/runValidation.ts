export type RunValidationBlock = {
  block_type: string;
  label: string;
  parameters?: Array<unknown> | null;
  parameter_keys?: Array<unknown> | null;
  loop_blocks?: Array<RunValidationBlock> | null;
};

export type LoginBlockWithoutCredentials = { label: string };

const CREDENTIAL_PARAMETER_TYPES = new Set([
  "credential",
  "onepassword",
  "bitwarden_login_credential",
  "azure_vault_credential",
]);

// Resolves a parameter key to whether it names a credential. The editor knows
// only the block's keys, so it supplies the lookup from the workflow's
// parameter definitions.
export type CredentialKeyPredicate = (key: string) => boolean;

// A login block's credential shares its parameter list with anything added via
// Advanced > Parameters, so a non-empty list does not mean the credential slot
// is still filled.
export function isCredentialParameter(parameter: unknown): boolean {
  if (typeof parameter !== "object" || parameter === null) {
    return false;
  }
  const record = parameter as Record<string, unknown>;
  const type = record.parameter_type ?? record.parameterType;
  if (typeof type === "string" && CREDENTIAL_PARAMETER_TYPES.has(type)) {
    return true;
  }
  // A workflow input can carry a credential id instead of a credential param.
  // The persisted parameter names this `workflow_parameter_type`; the editor
  // renames it to `dataType` (see editor/utils.ts), so both spellings reach
  // here — the run form reads persisted blocks, the canvas reads editor state.
  const dataType =
    record.data_type ?? record.dataType ?? record.workflow_parameter_type;
  return type === "workflow" && dataType === "credential_id";
}

function loginBlockHasCredential(
  block: RunValidationBlock,
  isCredentialKey?: CredentialKeyPredicate,
): boolean {
  if ("parameters" in block) {
    return (block.parameters ?? []).some(isCredentialParameter);
  }
  if (!isCredentialKey) {
    return false;
  }
  return (block.parameter_keys ?? []).some(
    (key) => typeof key === "string" && isCredentialKey(key),
  );
}

export function isLoginBlockMissingCredentials(
  block: RunValidationBlock,
  isCredentialKey?: CredentialKeyPredicate,
): boolean {
  return (
    block.block_type === "login" &&
    !loginBlockHasCredential(block, isCredentialKey)
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
  isCredentialKey?: CredentialKeyPredicate,
): Array<LoginBlockWithoutCredentials> {
  const result: Array<LoginBlockWithoutCredentials> = [];

  for (const block of blocks) {
    if (isLoginBlockMissingCredentials(block, isCredentialKey)) {
      result.push({ label: block.label });
    }

    if (isNestedLoopRunValidationBlock(block)) {
      result.push(
        ...getLoginBlocksWithoutCredentials(
          block.loop_blocks ?? [],
          isCredentialKey,
        ),
      );
    }
  }

  return result;
}
