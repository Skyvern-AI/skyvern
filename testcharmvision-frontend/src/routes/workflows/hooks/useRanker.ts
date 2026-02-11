import { useState, useMemo, useCallback } from "react";

interface RankedItems {
  [key: string]: number;
}

interface UseRankerReturn {
  rankedItems: RankedItems;
  promote: (name: string) => void;
  orderedNames: string[];
}

function useRanker(initialNames: string[]): UseRankerReturn {
  const [orderedNames, setOrderedNames] = useState<string[]>(initialNames);

  const rankedItems = useMemo<RankedItems>(() => {
    const items: RankedItems = {};
    const maxRank = orderedNames.length;

    orderedNames.forEach((name, index) => {
      items[name] = maxRank - index;
    });

    return items;
  }, [orderedNames]);

  const promote = useCallback((name: string) => {
    setOrderedNames((prevNames) => {
      if (!prevNames.includes(name)) {
        console.warn(`Name "${name}" not found in ranked list`);
        return prevNames;
      }

      const filteredNames = prevNames.filter((n) => n !== name);
      return [name, ...filteredNames];
    });
  }, []);

  return {
    rankedItems,
    promote,
    orderedNames,
  };
}

export { useRanker, type RankedItems };
