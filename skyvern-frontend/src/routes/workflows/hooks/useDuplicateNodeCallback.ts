import { BlockActionContext } from "@/store/BlockActionContext";
import { useContext } from "react";

function useDuplicateNodeCallback() {
  return useContext(BlockActionContext)?.duplicateNodeCallback;
}

export { useDuplicateNodeCallback };
