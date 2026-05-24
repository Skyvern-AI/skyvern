import { useMemo } from "react";

import { isMacPlatform } from "@/util/platform";

export function useUndoRedoShortcutLabels() {
  return useMemo(() => {
    const mac = isMacPlatform();
    return {
      undoShortcutLabel: mac ? "⌘Z" : "Ctrl+Z",
      redoShortcutLabel: mac ? "⌘⇧Z" : "Ctrl+Shift+Z",
    };
  }, []);
}
