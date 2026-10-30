import { createContext } from "react";

export const WorkflowDocActionsContext = createContext<{
  editable: boolean;
  openSettings: (id: string) => void;
  openVisual: () => void;
  goTo: (id: string) => void;
  selectedBranch: (conditionalId: string) => string | undefined;
  selectBranch: (conditionalId: string, branchId: string) => void;
  loopOpen: (loopId: string) => boolean;
  toggleLoop: (loopId: string) => void;
}>({
  editable: false,
  openSettings: () => {},
  openVisual: () => {},
  goTo: () => {},
  selectedBranch: () => undefined,
  selectBranch: () => {},
  loopOpen: () => true,
  toggleLoop: () => {},
});
