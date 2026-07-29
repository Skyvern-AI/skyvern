import { useCallback, useRef } from "react";
import type { Blocker } from "react-router-dom";

/**
 * Exits a react-router blocking episode at most once. Every reference a callback can
 * hold is a render snapshot (v6 exposes no live read), so one resuming after the router
 * already advanced still reads "blocked"; both exits latch rather than just proceed,
 * because reset is what moves the router and proceed is what then fires illegally into it.
 */
export function useBlockerExit(blocker: Blocker): {
  proceed: () => void;
  reset: () => void;
} {
  const blockerRef = useRef(blocker);
  blockerRef.current = blocker;

  const exitedRef = useRef(false);
  if (blocker.state === "unblocked") {
    exitedRef.current = false;
  }

  const exit = useCallback((action: "proceed" | "reset") => {
    if (exitedRef.current) {
      return;
    }
    const current = blockerRef.current;
    if (current.state !== "blocked") {
      return;
    }
    exitedRef.current = true;
    if (action === "proceed") {
      current.proceed();
    } else {
      current.reset();
    }
  }, []);

  const proceed = useCallback(() => exit("proceed"), [exit]);
  const reset = useCallback(() => exit("reset"), [exit]);

  return { proceed, reset };
}
