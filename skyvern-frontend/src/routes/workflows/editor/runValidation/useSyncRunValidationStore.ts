import { useEffect } from "react";

import type { AppNode } from "../nodes";
import { getRunBlockingBlocks } from "./getRunBlockingBlocks";
import { useRunValidationStore } from "./useRunValidationStore";

// Mirrors run-blocking blocks from the live canvas into the shared store; resets on unmount.
export function useSyncRunValidationStore(nodes: Array<AppNode>): void {
  const setBlockingBlocks = useRunValidationStore((s) => s.setBlockingBlocks);

  useEffect(() => {
    setBlockingBlocks(getRunBlockingBlocks(nodes));
  }, [nodes, setBlockingBlocks]);

  useEffect(() => {
    return () => setBlockingBlocks([]);
  }, [setBlockingBlocks]);
}
