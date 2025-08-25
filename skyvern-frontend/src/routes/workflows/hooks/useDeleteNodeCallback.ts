import { BlockActionContext } from "@/store/BlockActionContext";
import { useContext } from "react";

function useDeleteNodeCallback() {
  const deleteNodeCallback = useContext(BlockActionContext)?.deleteNodeCallback;

  if (!deleteNodeCallback) {
    throw new Error(
      "useDeleteNodeCallback must be used within a BlockActionContextProvider",
    );
  }

  return deleteNodeCallback;
}

export { useDeleteNodeCallback };
