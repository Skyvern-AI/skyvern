import { useCallback, useEffect, useRef, useState } from "react";
import { useDebouncedCallback } from "use-debounce";
import { flushSync } from "react-dom";
import {
  isEditorMutationLocked,
  selectEditorMutationLocked,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";

type DeferredEdit = {
  value: string;
  propValue: string;
  propChanged: boolean;
  workflowId?: string;
};

export const deferredEdits = new Map<string, DeferredEdit>();

const bufferedEditorFlushers = new Set<() => void>();

export function registerBufferedEditorFlusher(flush: () => void): () => void {
  bufferedEditorFlushers.add(flush);
  return () => {
    bufferedEditorFlushers.delete(flush);
  };
}

export function flushBufferedEditorEdits() {
  if (isEditorMutationLocked()) return;
  // Save-data callbacks are registered by effects after the graph renders.
  flushSync(() => {
    for (const flush of [...bufferedEditorFlushers]) flush();
  });
}

const deferredEditReplayRevisions = new Map<string, number>();

export function getDeferredEditReplayRevision(workflowId: string | undefined) {
  return workflowId === undefined
    ? 0
    : (deferredEditReplayRevisions.get(workflowId) ?? 0);
}

export function recordDeferredEditReplay(workflowId: string | undefined) {
  if (workflowId !== undefined) {
    deferredEditReplayRevisions.set(
      workflowId,
      getDeferredEditReplayRevision(workflowId) + 1,
    );
  }
}

function workflowIdFromDeferredKey(deferKey?: string): string | undefined {
  if (deferKey === undefined) return;
  try {
    const key: unknown = JSON.parse(deferKey);
    if (Array.isArray(key) && typeof key[0] === "string") return key[0];
  } catch {
    // Legacy unscoped keys do not identify a workflow's mount baseline.
  }
}

export function clearDeferredEdits(workflowId?: string) {
  if (workflowId === undefined) {
    deferredEdits.clear();
    return;
  }
  const fieldKeyPrefix = `[${JSON.stringify(workflowId)},`;
  for (const key of deferredEdits.keys()) {
    if (
      key.startsWith(fieldKeyPrefix) ||
      key === `${workflowId}:title` ||
      key.startsWith("[null,") ||
      key.startsWith('["__global__",')
    ) {
      deferredEdits.delete(key);
    }
  }
}

export function useDeferredLockedEdit({
  value,
  onChange,
  debounceMs = 300,
  deferKey,
  mutationLockEnabled = true,
}: {
  value: string;
  onChange?: (value: string) => void;
  debounceMs?: number;
  deferKey?: string;
  mutationLockEnabled?: boolean;
}) {
  const mutationLocked = useWorkflowYamlEditorStore(
    (state) => mutationLockEnabled && selectEditorMutationLocked(state),
  );
  const isMutationLocked = useCallback(
    () => mutationLockEnabled && isEditorMutationLocked(),
    [mutationLockEnabled],
  );
  const [internalValue, setInternalValue] = useState(value);
  const previousPropRef = useRef(value);
  const previousKeyRef = useRef(deferKey);
  const pendingRef = useRef<string | null>(null);
  const deferredRef = useRef<DeferredEdit | null>(null);
  const skipPropSyncRef = useRef(false);
  const onChangeRef = useRef(onChange);

  useEffect(() => {
    onChangeRef.current = onChange;
  }, [onChange]);

  const debouncedOnChange = useDebouncedCallback((newValue: string) => {
    // The store can lock before the component renders its disabled state.
    if (isMutationLocked()) return;
    pendingRef.current = null;
    if (deferKey !== undefined) deferredEdits.delete(deferKey);
    onChangeRef.current?.(newValue);
  }, debounceMs);

  useEffect(() => {
    if (deferKey === undefined) return;
    return () => {
      const workflowId = workflowIdFromDeferredKey(deferKey);
      if (
        !isMutationLocked() &&
        (workflowId === undefined ||
          !useWorkflowYamlEditorStore.getState().pendingSaves[workflowId])
      )
        deferredEdits.delete(deferKey);
      debouncedOnChange.cancel();
      pendingRef.current = null;
      deferredRef.current = null;
      skipPropSyncRef.current = false;
    };
  }, [deferKey, debouncedOnChange, isMutationLocked]);

  useEffect(() => {
    if (deferKey !== previousKeyRef.current) {
      setInternalValue(value);
      previousKeyRef.current = deferKey;
    }
    let deferred =
      deferKey !== undefined
        ? deferredEdits.get(deferKey)
        : deferredRef.current;
    if (deferred) {
      deferred.propChanged = value !== deferred.propValue;
    }
    if (mutationLocked) {
      debouncedOnChange.cancel();
      skipPropSyncRef.current = false;
      if (pendingRef.current !== null) {
        deferred ??= {
          value: pendingRef.current,
          propValue: previousPropRef.current,
          propChanged: false,
        };
        if (deferKey !== undefined) deferredEdits.set(deferKey, deferred);
        else deferredRef.current = deferred;
        pendingRef.current = null;
      }
      if (!deferred) {
        setInternalValue(value);
      }
    } else if (deferred && pendingRef.current === null) {
      if (deferKey !== undefined) deferredEdits.delete(deferKey);
      deferredRef.current = null;
      if (!deferred.propChanged) {
        setInternalValue(deferred.value);
        if (onChangeRef.current) {
          recordDeferredEditReplay(workflowIdFromDeferredKey(deferKey));
          onChangeRef.current(deferred.value);
        }
      } else {
        setInternalValue(value);
      }
    } else if (value !== previousPropRef.current) {
      debouncedOnChange.cancel();
      pendingRef.current = null;
      if (deferKey !== undefined) deferredEdits.delete(deferKey);
      // Immediate inputs keep the caret stable through their parent update.
      if (!skipPropSyncRef.current) setInternalValue(value);
      skipPropSyncRef.current = false;
    }
    previousPropRef.current = value;
  }, [mutationLocked, value, debouncedOnChange, deferKey]);

  const handleChange = useCallback(
    (newValue: string) => {
      if (isMutationLocked()) return;
      pendingRef.current = newValue;
      // Persist before a lock can unmount this input without running its effect.
      if (deferKey !== undefined) {
        deferredEdits.set(deferKey, {
          value: newValue,
          propValue: previousPropRef.current,
          propChanged: false,
        });
      }
      setInternalValue(newValue);
      debouncedOnChange(newValue);
      if (debounceMs === 0) {
        skipPropSyncRef.current = true;
        debouncedOnChange.flush();
      }
    },
    [debounceMs, debouncedOnChange, deferKey, isMutationLocked],
  );

  const handleBlur = useCallback(() => {
    if (!isMutationLocked()) debouncedOnChange.flush();
  }, [debouncedOnChange, isMutationLocked]);

  useEffect(() => {
    bufferedEditorFlushers.add(handleBlur);
    return () => {
      bufferedEditorFlushers.delete(handleBlur);
    };
  }, [handleBlur]);

  return {
    value: internalValue,
    onChange: handleChange,
    onBlur: handleBlur,
    mutationLocked,
  };
}
