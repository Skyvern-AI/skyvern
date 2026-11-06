import type { AppNode } from "../nodes";
import type { DocItem } from "./buildDocTree";
import { docStepTitle } from "./docStepRegistry";

export type DocOutlineEntry = { id: string; depth: number; title: string };

export function docOutlineEntries(
  items: DocItem[],
  byId: Map<string, AppNode>,
) {
  const entries: DocOutlineEntry[] = [];
  const add = (list: DocItem[], depth: number) => {
    for (const item of list) {
      if (item.kind === "note") continue;
      if (item.kind === "unplaced") {
        for (const id of item.nodeIds) {
          const node = byId.get(id);
          if (node) entries.push({ id, depth, title: docStepTitle(node) });
        }
        continue;
      }
      const node = byId.get(item.nodeId);
      if (!node) continue;
      entries.push({ id: item.nodeId, depth, title: docStepTitle(node) });
      if (item.kind === "loop") add(item.children, depth + 1);
      if (item.kind === "conditional")
        item.branches.forEach((branch) => add(branch.children, depth + 1));
    }
  };
  add(items, 0);
  return entries;
}
