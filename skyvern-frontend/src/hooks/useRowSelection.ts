import { useCallback, useEffect, useRef, useState } from "react";

type UseRowSelectionOptions<T> = {
  items: T[];
  getId: (item: T) => string;
  // Selection and anchor clear whenever this changes; compose tuples with JSON.stringify.
  resetKey?: unknown;
  // Anchor-only reset (selection survives), e.g. page-size changes shift row indices.
  anchorResetKey?: unknown;
};

function useRowSelection<T>({
  items,
  getId,
  resetKey,
  anchorResetKey,
}: UseRowSelectionOptions<T>) {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const anchorIndex = useRef<number | null>(null);

  useEffect(() => {
    setSelected(new Set());
    anchorIndex.current = null;
  }, [resetKey]);

  useEffect(() => {
    anchorIndex.current = null;
  }, [anchorResetKey]);

  const selectedItems = items.filter((item) => selected.has(getId(item)));
  const allSelected =
    items.length > 0 && items.every((item) => selected.has(getId(item)));
  const someSelected = selected.size > 0 && !allSelected;
  const indexById = new Map(items.map((item, index) => [getId(item), index]));

  function isSelected(id: string) {
    return selected.has(id);
  }

  function handleSelect(index: number, shiftKey: boolean) {
    const item = items[index];
    if (!item) {
      return;
    }
    const id = getId(item);
    if (shiftKey && anchorIndex.current !== null) {
      const anchor = Math.min(anchorIndex.current, items.length - 1);
      const start = Math.min(anchor, index);
      const end = Math.max(anchor, index);
      setSelected((prev) => {
        const rangeIds: string[] = [];
        for (let i = start; i <= end; i++) {
          rangeIds.push(getId(items[i]!));
        }
        const allInRangeSelected = rangeIds.every((rangeId) =>
          prev.has(rangeId),
        );
        const next = new Set(prev);
        for (const rangeId of rangeIds) {
          if (allInRangeSelected) {
            next.delete(rangeId);
          } else {
            next.add(rangeId);
          }
        }
        return next;
      });
      return;
    }
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
    anchorIndex.current = index;
  }

  function toggleSelectAll() {
    if (allSelected) {
      setSelected(new Set());
    } else {
      setSelected(new Set(items.map(getId)));
    }
  }

  const clearSelection = useCallback(() => {
    setSelected(new Set());
    anchorIndex.current = null;
  }, []);

  const replaceSelection = useCallback((ids: Iterable<string>) => {
    setSelected(new Set(ids));
    anchorIndex.current = null;
  }, []);

  return {
    selected: selected as ReadonlySet<string>,
    selectedItems,
    isSelected,
    allSelected,
    someSelected,
    indexById: indexById as ReadonlyMap<string, number>,
    handleSelect,
    toggleSelectAll,
    clearSelection,
    replaceSelection,
  };
}

export { useRowSelection };
