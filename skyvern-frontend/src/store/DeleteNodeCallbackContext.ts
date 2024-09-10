import { createContext } from "react";

type DeleteNodeCallback = (id: string) => void;

const DeleteNodeCallbackContext = createContext<DeleteNodeCallback | undefined>(
  undefined,
);

export { DeleteNodeCallbackContext };
