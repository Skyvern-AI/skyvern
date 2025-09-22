/**
 * UI Store: put UI-only state here, that needs to be shared across components, tabs, and
 * potentially browser refreshes.
 */

import { create } from "zustand";

const namespace = "skyvern.ui" as const;

const write = (key: string, value: unknown) => {
  try {
    const serialized = JSON.stringify(value);
    localStorage.setItem(makeKey(key), serialized);
  } catch (error) {
    console.error("Error writing to localStorage:", error);
  }
};

const read = <T>(
  key: string,
  validator: (v: T) => boolean,
  defaultValue: T,
): T => {
  try {
    const serialized = localStorage.getItem(makeKey(key));

    if (serialized === null) {
      return defaultValue;
    }

    const value = JSON.parse(serialized) as T;

    if (validator(value)) {
      return value;
    }

    return defaultValue;
  } catch (error) {
    return defaultValue;
  }
};

const makeKey = (name: string) => {
  return `${namespace}.${name}`;
};

type UiStore = {
  highlightGenerateCodeToggle: boolean;
  setHighlightGenerateCodeToggle: (v: boolean) => void;
};

/**
 * There's gotta be a way to remove this boilerplate and keep type-safety (no time)...
 */
const useUiStore = create<UiStore>((set) => {
  return {
    highlightGenerateCodeToggle: read(
      makeKey("highlightGenerateCodeToggle"),
      (v) => typeof v === "boolean",
      false,
    ),
    setHighlightGenerateCodeToggle: (v: boolean) => {
      set({ highlightGenerateCodeToggle: v });
      write(makeKey("highlightGenerateCodeToggle"), v);
    },
  };
});

export { useUiStore };
