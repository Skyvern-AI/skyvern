import { isLoginNode } from "../nodes/LoginNode/types";
import type { AppNode } from "../nodes";
import { isLoginBlockMissingCredentials } from "../../runValidation";
import {
  resolveBlockLocation,
  type RunBlockingPathSegment,
} from "./resolveBlockPath";

// The only run-blocking rule today is a login block missing a credential.
export const RUN_BLOCKING_REASON = "Login · needs a credential";

export type RunBlockingBlock = {
  id: string;
  label: string;
  // Loop/conditional ancestor node ids, so a collapsed container can show a
  // rolled-up count of the blocks that need fixing inside it.
  ancestorIds: Array<string>;
  // Breadcrumb of container ancestors, outermost first; empty for top-level blocks.
  path: Array<RunBlockingPathSegment>;
};

// Login blocks missing a credential block a run (never a save). Nested logins
// (inside loops / conditional branches) live flat in the node array, so a
// single scan already counts them; resolveBlockLocation recovers where each lives.
export function getRunBlockingBlocks(
  nodes: Array<AppNode>,
): Array<RunBlockingBlock> {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  return nodes
    .filter(isLoginNode)
    .filter((node) =>
      isLoginBlockMissingCredentials({
        block_type: "login",
        label: node.data.label,
        url: node.data.url,
        parameter_keys: node.data.parameterKeys,
      }),
    )
    .map((node) => {
      const { ancestorIds, path } = resolveBlockLocation(byId, node.id);
      return { id: node.id, label: node.data.label, ancestorIds, path };
    });
}
