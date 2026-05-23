import { createContext, useContext } from "react";

type CollapseContextValue = {
  open: boolean;
};

export const CollapseContext = createContext<CollapseContextValue>({
  open: true,
});

export function useCollapseContext(): CollapseContextValue {
  return useContext(CollapseContext);
}
