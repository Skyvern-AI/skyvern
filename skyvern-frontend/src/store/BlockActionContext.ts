import { createContext } from "react";

type DeleteNodeCallback = (id: string) => void;
type ToggleScriptForNodeCallback = (opts: {
  id?: string;
  label?: string;
  show: boolean;
}) => void;

const BlockActionContext = createContext<
  | {
      deleteNodeCallback: DeleteNodeCallback;
      toggleScriptForNodeCallback?: ToggleScriptForNodeCallback;
    }
  | undefined
>(undefined);

export { BlockActionContext };
