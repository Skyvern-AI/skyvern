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
    const combined = new Set<string>();
    // Symmetric difference (XOR): a row is expanded if it's in one set but not both.
    // This lets manual toggles override auto-expansion (and vice versa).
    for (const id of autoExpandedRows) {
      if (!manuallyExpandedRows.has(id)) {
        combined.add(id);
      }
    }
    for (const id of manuallyExpandedRows) {
      if (!autoExpandedRows.has(id)) {
        combined.add(id);
      }
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
