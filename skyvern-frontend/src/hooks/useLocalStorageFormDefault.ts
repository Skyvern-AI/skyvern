/**
 * Returns a value from localStorage for the given key, or a fallback if not present.
 * Use this hook to initialize form default values from localStorage in a type-safe way.
 *
 * @param storageKey - The localStorage key to read
 * @param fallback - The fallback value if localStorage is empty or unavailable
 * @returns The value from localStorage (if present), otherwise the fallback
 */
import { useMemo } from "react";

export function useLocalStorageFormDefault(
  storageKey: string,
  fallback: string | null | undefined,
): string | null | undefined {
  return useMemo(() => {
    if (typeof window === "undefined") return fallback ?? null;
    const value = localStorage.getItem(storageKey);
    return value !== null ? value : fallback ?? null;
  }, [storageKey, fallback]);
}
