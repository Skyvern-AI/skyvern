import { useState } from "react";

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

  const bump = () => {
    setTimeout(() => {
      setForceRenderKey((prev) => prev + 1);
    }, delay);
  };

  return {
    bump,
    key: `${prefix}-${forceRenderKey}`,
  };
};

export { useRerender };
