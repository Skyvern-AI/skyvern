import { useEffect } from "react";

export function useTurnActivityChange(
  active: boolean,
  onTurnActivityChange?: (active: boolean) => void,
) {
  useEffect(() => {
    onTurnActivityChange?.(active);
  }, [active, onTurnActivityChange]);

  useEffect(
    () => () => {
      onTurnActivityChange?.(false);
    },
    [onTurnActivityChange],
  );
}
