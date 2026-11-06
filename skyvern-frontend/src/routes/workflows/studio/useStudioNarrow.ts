import { useSyncExternalStore } from "react";

// Below this viewport width the studio shows one pane at a time.
export const STUDIO_NARROW_QUERY = "(max-width: 719px)";

function subscribe(onChange: () => void) {
  if (typeof window.matchMedia !== "function") return () => {};
  const query = window.matchMedia(STUDIO_NARROW_QUERY);
  query.addEventListener("change", onChange);
  return () => query.removeEventListener("change", onChange);
}

export function isStudioNarrow(): boolean {
  return (
    typeof window.matchMedia === "function" &&
    window.matchMedia(STUDIO_NARROW_QUERY).matches
  );
}

export function useStudioNarrow(): boolean {
  return useSyncExternalStore(subscribe, isStudioNarrow, () => false);
}
