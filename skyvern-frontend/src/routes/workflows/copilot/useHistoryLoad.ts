import { useCallback, useRef, useState } from "react";

/**
 * The chat-history loading flag, owned together with the sequence number that says WHICH load
 * owns it.
 *
 * The flag and the sequence are not separable: `beginHistoryLoad` is the only way to raise the flag
 * and the only source of a sequence number, so a claimant cannot take one without the other, and a
 * releaser that never began cannot type-check. A releaser that clears unconditionally lets a
 * superseded load switch the chat's actions back on while a newer load is still in flight.
 *
 * The comparison lives HERE and callers release unconditionally: a caller writing
 * `if (seq === current) endHistoryLoad(seq)` would put the same forgettable check in a new place.
 */
export function useHistoryLoad(): {
  isLoadingHistory: boolean;
  beginHistoryLoad: () => number;
  endHistoryLoad: (seq: number) => void;
} {
  const [isLoadingHistory, setIsLoadingHistory] = useState(false);
  const seqRef = useRef(0);

  const beginHistoryLoad = useCallback((): number => {
    seqRef.current += 1;
    setIsLoadingHistory(true);
    return seqRef.current;
  }, []);

  const endHistoryLoad = useCallback((seq: number): void => {
    // Only the load that still owns the flag may lower it.
    if (seqRef.current !== seq) {
      return;
    }
    setIsLoadingHistory(false);
  }, []);

  return { isLoadingHistory, beginHistoryLoad, endHistoryLoad };
}
