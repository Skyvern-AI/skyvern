import { BlockActionContext } from "@/store/BlockActionContext";
import { useContext } from "react";

function useRequestDeleteNodeCallback() {
  const requestDeleteNodeCallback =
    useContext(BlockActionContext)?.requestDeleteNodeCallback;

  if (!requestDeleteNodeCallback) {
    throw new Error(
      "useRequestDeleteNodeCallback must be used within a BlockActionContextProvider",
    );
  }

  return requestDeleteNodeCallback;
}

export { useRequestDeleteNodeCallback };
