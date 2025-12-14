import { BlockActionContext } from "@/store/BlockActionContext";
import { useContext } from "react";

function useTransmuteNodeCallback() {
  const transmuteNodeCallback =
    useContext(BlockActionContext)?.transmuteNodeCallback;

  if (!transmuteNodeCallback) {
    throw new Error(
      "useTransmuteNodeCallback must be used within a BlockActionContextProvider",
    );
  }

  return transmuteNodeCallback;
}

export { useTransmuteNodeCallback };
