import type { KeyboardEvent } from "react";

export const MAX_KEYED_CHOICES = 9;

// The item a bare digit key picks from a tray's keyed list, or null for any other key.
export function keyedChoiceFor<T>(
  event: KeyboardEvent<HTMLElement>,
  keyed: readonly T[],
): T | null {
  if (event.metaKey || event.ctrlKey || event.altKey) return null;
  if (!/^[1-9]$/.test(event.key)) return null;
  return keyed[Number(event.key) - 1] ?? null;
}
