import { useCallback, useEffect, useRef, useState } from "react";

/**
 * ```tsx
 * const { bump, key } = useRerender({ delay: 40, prefix: "my-prefix" });
 *
 * <div key={key}>...</div>
 *
 * // somewhere else
 * bump();
 * ```
 */
const useRerender = ({
  delay = 40,
  prefix,
}: {
  delay?: number;
  prefix: string;
}) => {
  const [forceRenderKey, setForceRenderKey] = useState(0);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const delayRef = useRef(delay);
  delayRef.current = delay;

  const bump = useCallback(() => {
    if (timeoutRef.current !== null) {
      clearTimeout(timeoutRef.current);
    }
    timeoutRef.current = setTimeout(() => {
      timeoutRef.current = null;
      setForceRenderKey((prev) => prev + 1);
    }, delayRef.current);
  }, []);

  // Cancel any pending bump on unmount so the setForceRenderKey call cannot
  // fire after the component (or its test environment) has been torn down.
  useEffect(() => {
    return () => {
      if (timeoutRef.current !== null) {
        clearTimeout(timeoutRef.current);
        timeoutRef.current = null;
      }
    };
  }, []);

  return {
    bump,
    key: `${prefix}-${forceRenderKey}`,
  };
};

export { useRerender };
