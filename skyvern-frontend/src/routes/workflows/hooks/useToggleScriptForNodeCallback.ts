import { BlockActionContext } from "@/store/BlockActionContext";
import { useContext } from "react";

function useToggleScriptForNodeCallback() {
  const toggleScriptForNodeCallback =
    useContext(BlockActionContext)?.toggleScriptForNodeCallback;

  if (!toggleScriptForNodeCallback) {
    throw new Error(
      "useToggleScriptForNodeCallback must be used within a BlockActionContextProvider",
    );
  }

  return toggleScriptForNodeCallback;
}

export { useToggleScriptForNodeCallback };
