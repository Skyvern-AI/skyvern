import { createContext } from "react";

type DeleteNodeCallback = (id: string) => void;
type TransmuteNodeCallback = (id: string, nodeName: string) => void;
type ToggleScriptForNodeCallback = (opts: {
  id?: string;
  label?: string;
  show: boolean;
}) => void;

const BlockActionContext = createContext<
  | {
      deleteNodeCallback: DeleteNodeCallback;
      transmuteNodeCallback: TransmuteNodeCallback;
      toggleScriptForNodeCallback?: ToggleScriptForNodeCallback;
    }
  | undefined
>(undefined);

export { BlockActionContext };
