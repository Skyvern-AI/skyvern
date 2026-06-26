import { useEffect, useState } from "react";

/**
 * Keep an element mounted through its exit animation: when `open` flips false it
 * stays mounted for `exitMs` so the animation can play, then unmounts.
 */
export function usePresence(open: boolean, exitMs = 150): boolean {
  const [present, setPresent] = useState(open);

  useEffect(() => {
    if (open) {
      setPresent(true);
      return;
    }
    const timer = window.setTimeout(() => setPresent(false), exitMs);
    return () => window.clearTimeout(timer);
  }, [open, exitMs]);

  return present;
}
