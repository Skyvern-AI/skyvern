import { useCallback, useState } from "react";

function useParameterExpansion() {
  const [expandedRows, setExpandedRows] = useState<Set<string>>(new Set());

  const toggleExpanded = useCallback((id: string) => {
    setExpandedRows((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }, []);

  return {
    expandedRows,
    toggleExpanded,
  };
}

export { useParameterExpansion };
