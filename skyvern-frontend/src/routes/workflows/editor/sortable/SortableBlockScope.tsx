import {
  SortableContext,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { type PropsWithChildren } from "react";

import { getScopeKey, type SortableBlockScopeDescriptor } from "./scope";

type SortableBlockScopeProps = PropsWithChildren<{
  scope: SortableBlockScopeDescriptor;
  items: Array<string>;
}>;

/**
 * Scope-keyed wrapper around dnd-kit's SortableContext. Every scope declares
 * its sibling block ids and a stable id so drops are routed to the right
 * sortable list. M1 mounts a single top-level instance; M2 mounts one per
 * loop and per conditional branch.
 *
 * Earlier revisions wrapped this in `memo` with a deep-by-content
 * comparator. The optimization was inert: `FlowRenderer` passes inline JSX
 * children (the ReactFlow subtree) on every render, so the children
 * identity always changed and the memo short-circuited to `false` before
 * the deep `scope` / `items` checks ever ran. Dropping the memo keeps the
 * file smaller and removes a misleading "this is memoized" cue. If a
 * future regression shows hot per-frame work under SortableContext, fix
 * it by stabilising the children identity at the `FlowRenderer` call
 * site (e.g. wrap the ReactFlow subtree in `useMemo`) and re-add memo
 * here — the deep comparator below would then actually save work.
 */
function SortableBlockScope({
  scope,
  items,
  children,
}: SortableBlockScopeProps) {
  return (
    <SortableContext
      id={getScopeKey(scope)}
      items={items}
      strategy={verticalListSortingStrategy}
    >
      {children}
    </SortableContext>
  );
}

export { SortableBlockScope };
