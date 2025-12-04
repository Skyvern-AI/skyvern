import { useCallback, useMemo } from "react";
import type { ParameterDisplayItem } from "../components/ParameterDisplayInline";

function normalize(value: unknown): string {
  if (value === null || value === undefined) {
    return "";
  }

  if (typeof value === "string") {
    return value;
  }

  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function useKeywordSearch(search: string) {
  const normalizedSearch = useMemo(() => search.trim().toLowerCase(), [search]);

  const isSearchActive = normalizedSearch.length > 0;

  const matchesText = useCallback(
    (text: string | null | undefined) => {
      if (!isSearchActive || !text) {
        return false;
      }
      return text.toLowerCase().includes(normalizedSearch);
    },
    [isSearchActive, normalizedSearch],
  );

  const matchesParameter = useCallback(
    (parameter: ParameterDisplayItem) => {
      if (!isSearchActive) {
        return false;
      }

      const valueString = normalize(parameter.value);

      return (
        matchesText(parameter.key) ||
        matchesText(parameter.description ?? "") ||
        matchesText(valueString)
      );
    },
    [isSearchActive, matchesText],
  );

  return {
    searchQuery: normalizedSearch,
    isSearchActive,
    matchesText,
    matchesParameter,
  };
}

export { useKeywordSearch };
