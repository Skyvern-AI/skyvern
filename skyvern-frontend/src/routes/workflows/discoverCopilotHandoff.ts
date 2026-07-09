import { useCallback, useEffect, useRef, useState } from "react";

const DISCOVER_COPILOT_HANDOFF_STORAGE_PREFIX =
  "skyvern.discoverCopilotHandoff";

function storageKey(workflowPermanentId: string): string {
  return `${DISCOVER_COPILOT_HANDOFF_STORAGE_PREFIX}:${workflowPermanentId}`;
}

function getSessionStorage(): Storage | null {
  if (typeof window === "undefined") {
    return null;
  }
  return window.sessionStorage;
}

function recoveryWorkflowId(
  shouldRead: boolean,
  workflowPermanentId: string | undefined,
): string | null {
  return shouldRead && workflowPermanentId ? workflowPermanentId : null;
}

export function rememberDiscoverCopilotPrompt(
  workflowPermanentId: string | undefined,
  prompt: string,
): void {
  if (!workflowPermanentId || !prompt) {
    return;
  }

  try {
    getSessionStorage()?.setItem(storageKey(workflowPermanentId), prompt);
  } catch {
    return;
  }
}

export function readDiscoverCopilotPrompt(
  workflowPermanentId: string | undefined,
): string | null {
  if (!workflowPermanentId) {
    return null;
  }

  // sessionStorage access can throw SecurityError in some private-browsing
  // modes; the catch below degrades to the pre-fix manual-retype path.
  try {
    const storage = getSessionStorage();
    if (!storage) {
      return null;
    }
    return storage.getItem(storageKey(workflowPermanentId));
  } catch {
    return null;
  }
}

export function useDiscoverCopilotPromptRecovery({
  shouldRead,
  workflowPermanentId,
}: {
  shouldRead: boolean;
  workflowPermanentId: string | undefined;
}) {
  const initialRecoveryWorkflowId = recoveryWorkflowId(
    shouldRead,
    workflowPermanentId,
  );
  const recoveryWorkflowIdRef = useRef(initialRecoveryWorkflowId);
  const [storedInitialCopilotMessage, setStoredInitialCopilotMessage] =
    useState(() =>
      initialRecoveryWorkflowId
        ? readDiscoverCopilotPrompt(initialRecoveryWorkflowId)
        : null,
    );

  useEffect(() => {
    const nextRecoveryWorkflowId = recoveryWorkflowId(
      shouldRead,
      workflowPermanentId,
    );
    if (recoveryWorkflowIdRef.current === nextRecoveryWorkflowId) {
      return;
    }
    recoveryWorkflowIdRef.current = nextRecoveryWorkflowId;
    setStoredInitialCopilotMessage(
      nextRecoveryWorkflowId
        ? readDiscoverCopilotPrompt(nextRecoveryWorkflowId)
        : null,
    );
  }, [shouldRead, workflowPermanentId]);

  const clearStoredInitialCopilotMessage = useCallback(() => {
    recoveryWorkflowIdRef.current = null;
    setStoredInitialCopilotMessage(null);
    forgetDiscoverCopilotPrompt(workflowPermanentId);
  }, [workflowPermanentId]);

  return {
    clearStoredInitialCopilotMessage,
    storedInitialCopilotMessage,
  };
}

// Drops `via=discover` from a location.search string once the seed prompt it
// pointed at has been consumed, so a later remount of the editor (e.g. via
// browser back/forward) can't make useViaEntryPointCapture re-fire
// copilot.discover.started for a turn that already happened.
export function withoutDiscoverViaParam(search: string): string {
  const params = new URLSearchParams(search);
  if (params.get("via") !== "discover") {
    return search;
  }
  params.delete("via");
  const next = params.toString();
  return next ? `?${next}` : "";
}

export function forgetDiscoverCopilotPrompt(
  workflowPermanentId: string | undefined,
): void {
  if (!workflowPermanentId) {
    return;
  }

  try {
    getSessionStorage()?.removeItem(storageKey(workflowPermanentId));
  } catch {
    return;
  }
}
