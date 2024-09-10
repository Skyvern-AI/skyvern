import { DeleteNodeCallbackContext } from "@/store/DeleteNodeCallbackContext";
import { useContext } from "react";

function useDeleteNodeCallback() {
  const deleteNodeCallback = useContext(DeleteNodeCallbackContext);

  if (!deleteNodeCallback) {
    throw new Error(
      "useDeleteNodeCallback must be used within a DeleteNodeCallbackProvider",
    );
  }

  return deleteNodeCallback;
}

export { useDeleteNodeCallback };
