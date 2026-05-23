import type { KeyboardCoordinateGetter } from "@dnd-kit/core";
import { sortableKeyboardCoordinates } from "@dnd-kit/sortable";

// Mirror of the pointer-path collision filtering for the keyboard sensor.
// dnd-kit's `sortableKeyboardCoordinates` derives the active block's container
// id from the nearest ancestor `SortableContext` in the React tree. In this
// editor every per-node `useSortable` resolves to the top-level scope because
// the nested `SortableBlockScope` mounts are siblings, not ancestors, of the
// ReactFlow node subtree. Without filtering, arrow-key reorders inside a loop
// or conditional branch would consider top-level siblings as candidates and
// resolve against the wrong list. Filter droppable containers to the active
// block's scope first so the upstream getter only sees in-scope siblings.
export function createScopeAwareKeyboardCoordinates(
  scopeKeyForId: (id: string) => string,
): KeyboardCoordinateGetter {
  return (event, args) => {
    const active = args.context.active;
    if (!active) {
      return sortableKeyboardCoordinates(event, args);
    }
    const activeScopeKey = scopeKeyForId(String(active.id));
    const original = args.context.droppableContainers;

    // Proxy preserves `.get` / `.toArray` / iteration on the underlying
    // `DroppableContainersMap` while overriding `.getEnabled()`. Cloning the
    // map would force us to track every method the upstream library calls.
    const proxiedContainers = new Proxy(original, {
      get(target, prop, receiver) {
        if (prop === "getEnabled") {
          return () =>
            target
              .getEnabled()
              .filter(
                (entry) => scopeKeyForId(String(entry.id)) === activeScopeKey,
              );
        }
        const value = Reflect.get(target, prop, receiver);
        return typeof value === "function" ? value.bind(target) : value;
      },
    });

    return sortableKeyboardCoordinates(event, {
      ...args,
      context: {
        ...args.context,
        droppableContainers: proxiedContainers,
      },
    });
  };
}
