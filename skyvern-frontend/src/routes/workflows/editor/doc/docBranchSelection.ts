import type { DocBranch, DocItem } from "./buildDocTree";

export function selectedStepIds(items: DocItem[]): string[] {
  return items.flatMap((item) =>
    item.kind === "note"
      ? []
      : item.kind === "unplaced"
        ? item.nodeIds
        : [
            item.nodeId,
            ...(item.kind === "loop"
              ? selectedStepIds(item.children)
              : item.kind === "conditional"
                ? item.branches.flatMap((b) => selectedStepIds(b.children))
                : []),
          ],
  );
}

export const docBranchSelection = (
  item: Extract<DocItem, { kind: "conditional" }>,
  selectedId: string | undefined,
): DocBranch =>
  item.branches.find((b) => b.branchId === selectedId) ?? item.branches[0]!;
