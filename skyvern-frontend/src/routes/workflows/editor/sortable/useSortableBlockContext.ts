import { useContext } from "react";

import {
  SortableBlockContext,
  type SortableBlockValue,
} from "./sortableBlockContextValue";

/**
 * consumer hook for the `SortableBlockContext`. Split into its
 * own file so the hook, the context, and the provider component each have
 * an HMR boundary that satisfies `react-refresh/only-export-components`.
 */
function useSortableBlockContext(): SortableBlockValue {
  return useContext(SortableBlockContext);
}

export { useSortableBlockContext };
