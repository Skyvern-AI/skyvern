import { createContext } from "react";

type LayoutCallbackFunction = () => void;

const LayoutCallbackContext = createContext<LayoutCallbackFunction | undefined>(
  undefined,
);

export { LayoutCallbackContext };
