import { createContext } from "react";

type RequestDeleteNodeCallback = (id: string, label: string) => void;
type DuplicateNodeCallback = (id: string) => void;
type TransmuteNodeCallback = (id: string, nodeName: string) => void;
type ToggleScriptForNodeCallback = (opts: {
  id?: string;
  label?: string;
  show: boolean;
}) => void;

const BlockActionContext = createContext<
  | {
      requestDeleteNodeCallback: RequestDeleteNodeCallback;
      duplicateNodeCallback: DuplicateNodeCallback;
      transmuteNodeCallback: TransmuteNodeCallback;
      toggleScriptForNodeCallback?: ToggleScriptForNodeCallback;
    }
  | undefined
>(undefined);

export { BlockActionContext };
