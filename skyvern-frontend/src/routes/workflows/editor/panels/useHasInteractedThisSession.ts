import { useEffect, useState } from "react";

export const SESSION_INTERACTED_KEY = "skyvern.blockSidebarSessionInteracted";

const SCOPE_SELECTOR = '[data-testid="block-config-sidebar"]';

export function useHasInteractedThisSession(): boolean {
  const [interacted, setInteracted] = useState<boolean>(() => {
    try {
      return sessionStorage.getItem(SESSION_INTERACTED_KEY) === "true";
    } catch {
      return false;
    }
  });

  useEffect(() => {
    if (interacted) return;
    function handler(e: Event) {
      const t = e.target as HTMLElement | null;
      if (!t) return;
      if (!t.closest(SCOPE_SELECTOR)) return;
      setInteracted(true);
      try {
        sessionStorage.setItem(SESSION_INTERACTED_KEY, "true");
      } catch {
        /* noop */
      }
    }
    document.addEventListener("input", handler, true);
    document.addEventListener("change", handler, true);
    return () => {
      document.removeEventListener("input", handler, true);
      document.removeEventListener("change", handler, true);
    };
  }, [interacted]);

  return interacted;
}
