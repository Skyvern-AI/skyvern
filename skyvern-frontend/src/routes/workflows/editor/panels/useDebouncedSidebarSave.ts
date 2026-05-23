import { useCallback, useEffect, useRef } from "react";

import { useSidebarSaveStateStore } from "@/store/SidebarSaveStateStore";

// Records `lastUpdatedAt` for the sidebar footer 300ms after a block's form
// value changes. Backend persistence happens via Cmd+S (`useSaveWorkflow`);
// this hook is footer bookkeeping only. `commit()` flushes the pending
// debounce so block switches never leave a block with un-stamped edits.

type UseDebouncedSidebarSaveOptions<T> = {
  blockId: string;
  value: T;
  debounceMs?: number;
};

type UseDebouncedSidebarSaveResult = {
  commit: () => boolean;
};

const DEFAULT_DEBOUNCE_MS = 300;

function valuesEqual<T>(a: T, b: T): boolean {
  if (Object.is(a, b)) return true;
  if (
    a === null ||
    b === null ||
    typeof a !== "object" ||
    typeof b !== "object"
  ) {
    return false;
  }
  try {
    return JSON.stringify(a) === JSON.stringify(b);
  } catch {
    return false;
  }
}

function useDebouncedSidebarSave<T>({
  blockId,
  value,
  debounceMs = DEFAULT_DEBOUNCE_MS,
}: UseDebouncedSidebarSaveOptions<T>): UseDebouncedSidebarSaveResult {
  const valueRef = useRef(value);
  const blockIdRef = useRef(blockId);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const baselineValueRef = useRef<T>(value);
  const lastTrackedValueRef = useRef<T>(value);
  const isFirstRunRef = useRef(true);

  const setLastUpdatedAt = useSidebarSaveStateStore(
    (state) => state.setLastUpdatedAt,
  );

  useEffect(() => {
    valueRef.current = value;
  }, [value]);

  const clearTimer = useCallback(() => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  useEffect(() => {
    blockIdRef.current = blockId;
    isFirstRunRef.current = true;
    baselineValueRef.current = valueRef.current;
    lastTrackedValueRef.current = valueRef.current;
    clearTimer();
  }, [blockId, clearTimer]);

  const stamp = useCallback((): boolean => {
    const ts = Date.now();
    lastTrackedValueRef.current = valueRef.current;
    setLastUpdatedAt(blockIdRef.current, ts);
    return true;
  }, [setLastUpdatedAt]);

  useEffect(() => {
    if (isFirstRunRef.current) {
      isFirstRunRef.current = false;
      baselineValueRef.current = value;
      lastTrackedValueRef.current = value;
      return;
    }

    if (valuesEqual(value, lastTrackedValueRef.current)) {
      clearTimer();
      return;
    }

    // Revert to the original mount value: nothing has changed from the
    // user's perspective, so don't bump the footer timestamp.
    if (valuesEqual(value, baselineValueRef.current)) {
      clearTimer();
      return;
    }

    clearTimer();
    timerRef.current = setTimeout(() => {
      timerRef.current = null;
      stamp();
    }, debounceMs);
  }, [value, debounceMs, clearTimer, stamp]);

  useEffect(() => {
    return () => {
      clearTimer();
    };
  }, [clearTimer]);

  const commit = useCallback((): boolean => {
    clearTimer();
    if (valuesEqual(valueRef.current, lastTrackedValueRef.current)) {
      return true;
    }
    if (valuesEqual(valueRef.current, baselineValueRef.current)) {
      return true;
    }
    return stamp();
  }, [clearTimer, stamp]);

  return { commit };
}

export { useDebouncedSidebarSave, DEFAULT_DEBOUNCE_MS };
export type { UseDebouncedSidebarSaveOptions, UseDebouncedSidebarSaveResult };
