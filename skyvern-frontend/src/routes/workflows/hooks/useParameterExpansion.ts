import { useCallback, useMemo, useState } from "react";

function toSet(ids: Iterable<string>): Set<string> {
  return new Set(ids);
}

function useParameterExpansion() {
  const [manuallyExpandedRows, setManuallyExpandedRows] = useState<Set<string>>(
    new Set(),
  );
  const [autoExpandedRows, setAutoExpandedRows] = useState<Set<string>>(
    new Set(),
  );

  const toggleExpanded = useCallback((id: string) => {
    setManuallyExpandedRows((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }, []);

  const expandedRows = useMemo(() => {
    const combined = new Set(autoExpandedRows);
    for (const id of manuallyExpandedRows) {
      combined.add(id);
    }
    return combined;
  }, [autoExpandedRows, manuallyExpandedRows]);

  const updateAutoExpandedRows = useCallback((ids: Iterable<string>) => {
    setAutoExpandedRows(toSet(ids));
  }, []);

  return {
    expandedRows,
    toggleExpanded,
    setManuallyExpandedRows,
    manuallyExpandedRows,
    setAutoExpandedRows: updateAutoExpandedRows,
  };
}

export { useParameterExpansion };
