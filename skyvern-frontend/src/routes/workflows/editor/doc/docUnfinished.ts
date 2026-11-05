import { create } from "zustand";

type DocUnfinishedStore = {
  ids: ReadonlySet<string>;
  add: (id: string) => void;
  settle: (ids: Iterable<string>) => void;
  clear: () => void;
};

/** Steps added in this session that the user has not yet left, run or saved. Their problems stay quiet. */
export const useDocUnfinishedStore = create<DocUnfinishedStore>((set, get) => ({
  ids: new Set(),
  add: (id) => set({ ids: new Set(get().ids).add(id) }),
  settle: (ids) => {
    const next = new Set(get().ids);
    let changed = false;
    for (const id of ids) changed = next.delete(id) || changed;
    if (changed) set({ ids: next });
  },
  clear: () => {
    if (get().ids.size) set({ ids: new Set() });
  },
}));

export const isStepUnfinished = (id: string): boolean =>
  useDocUnfinishedStore.getState().ids.has(id);

const FLOATING =
  "[aria-label='Document panel'], [role='dialog'], [role='listbox'], [role='menu'], [data-radix-popper-content-wrapper]";

/** Unfinished steps that an interaction on `target` has moved away from. */
export function stepsLeftBy(
  ids: ReadonlySet<string>,
  target: EventTarget | null,
): string[] {
  const element = target instanceof Element ? target : null;
  if (element?.closest(FLOATING)) return [];
  return [...ids].filter(
    (id) => !element?.closest(`[data-doc-node-id="${CSS.escape(id)}"]`),
  );
}
