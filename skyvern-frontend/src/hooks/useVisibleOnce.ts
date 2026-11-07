import { useEffect, useRef, useState } from "react";

const DEFAULT_ROOT_MARGIN = "200px";

/**
 * Latches true once the referenced element comes within `rootMargin` of the
 * viewport, so gated work runs for what the user can see rather than for every
 * mounted instance. Starts true without `IntersectionObserver` so the gated
 * work stays unconditional rather than dead.
 */
function useVisibleOnce<T extends HTMLElement>(
  rootMargin: string = DEFAULT_ROOT_MARGIN,
) {
  const ref = useRef<T | null>(null);
  const [visible, setVisible] = useState(
    typeof IntersectionObserver === "undefined",
  );

  useEffect(() => {
    if (visible) return;
    const element = ref.current;
    if (!element) {
      setVisible(true);
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setVisible(true);
          observer.disconnect();
        }
      },
      { rootMargin },
    );
    observer.observe(element);
    return () => observer.disconnect();
  }, [visible, rootMargin]);

  return { ref, visible };
}

export { useVisibleOnce };
