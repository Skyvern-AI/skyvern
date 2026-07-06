import { isLoginNode } from "../nodes/LoginNode/types";
import type { AppNode } from "../nodes";
import { isLoginBlockMissingCredentials } from "../../runValidation";

export type RunBlockingBlock = { id: string; label: string };

// Login blocks missing a credential block a run (never a save); nested logins appear flat here, so no recursion.
export function getRunBlockingBlocks(
  nodes: Array<AppNode>,
): Array<RunBlockingBlock> {
  return nodes
    .filter(isLoginNode)
    .filter((node) =>
      isLoginBlockMissingCredentials({
        block_type: "login",
        label: node.data.label,
        parameter_keys: node.data.parameterKeys,
      }),
    )
    .map((node) => ({ id: node.id, label: node.data.label }));
}
