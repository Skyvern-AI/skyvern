import { useContext } from "react";
import { DebugStoreContext } from "./DebugStoreContext";

export function useDebugStore() {
  const ctx = useContext(DebugStoreContext);
  if (!ctx) {
    throw new Error("useDebugStore must be used within a DebugStoreProvider");
  }
  return ctx;
}
