import { useCallback, useEffect, useMemo } from "react";
import { create } from "zustand";

const MAX_ATTEMPTS = 2;
const WINDOW_MS = 30 * 60 * 1000; // 30 minutes
const STORAGE_KEY_PREFIX = "skyvern:debug-session-rate-limit";

interface RateLimitState {
  isRateLimited: boolean;
  recordAttempt: () => void;
  resetOnSuccess: () => void;
}

function getStorageKey(workflowPermanentId: string): string {
  return `${STORAGE_KEY_PREFIX}:${workflowPermanentId}`;
}

function getAttempts(workflowPermanentId: string): number[] {
  try {
    const key = getStorageKey(workflowPermanentId);
    const raw = localStorage.getItem(key);
    if (!raw) return [];
    const attempts: number[] = JSON.parse(raw);
    if (!Array.isArray(attempts)) return [];
    const now = Date.now();
    return attempts.filter((t) => now - t < WINDOW_MS);
  } catch {
    return [];
  }
}

function saveAttempts(workflowPermanentId: string, attempts: number[]): void {
  try {
    const key = getStorageKey(workflowPermanentId);
    localStorage.setItem(key, JSON.stringify(attempts));
  } catch {
    // Ignore storage errors (e.g. private browsing, quota exceeded)
  }
}

// Shared zustand store so all hook instances see the same state.
// Keyed by workflowPermanentId so each workflow has independent rate limiting.
interface RateLimitStore {
  attemptsByWorkflow: Record<string, number[]>;
  setAttempts: (wpid: string, attempts: number[]) => void;
}

const useRateLimitStore = create<RateLimitStore>((set) => ({
  attemptsByWorkflow: {},
  setAttempts: (wpid, attempts) =>
    set((state) => ({
      attemptsByWorkflow: { ...state.attemptsByWorkflow, [wpid]: attempts },
    })),
}));

// Track active expiry timers per workflow to avoid duplicates
const expiryTimers: Record<string, NodeJS.Timeout> = {};

function useBrowserSessionRateLimit(
  workflowPermanentId: string | undefined,
): RateLimitState {
  const storeAttempts = useRateLimitStore((state) =>
    workflowPermanentId
      ? state.attemptsByWorkflow[workflowPermanentId] ?? null
      : null,
  );
  const setAttempts = useRateLimitStore((state) => state.setAttempts);

  // Initialize store from localStorage on first mount for this workflow
  const attempts = useMemo(
    () =>
      storeAttempts ??
      (workflowPermanentId ? getAttempts(workflowPermanentId) : []),
    [storeAttempts, workflowPermanentId],
  );

  // Sync localStorage into the store on first access
  useEffect(() => {
    if (workflowPermanentId && storeAttempts === null) {
      const fromStorage = getAttempts(workflowPermanentId);
      setAttempts(workflowPermanentId, fromStorage);
    }
  }, [workflowPermanentId, storeAttempts, setAttempts]);

  // Schedule a re-render when the rate limit expires
  useEffect(() => {
    if (attempts.length < MAX_ATTEMPTS || !workflowPermanentId) return;

    // Only one timer per workflow across all hook instances
    if (expiryTimers[workflowPermanentId]) return;

    const oldest = Math.min(...attempts);
    const expiresAt = oldest + WINDOW_MS;
    const now = Date.now();
    const delay = expiresAt - now;

    if (delay <= 0) {
      const fresh = getAttempts(workflowPermanentId);
      setAttempts(workflowPermanentId, fresh);
      return;
    }

    expiryTimers[workflowPermanentId] = setTimeout(() => {
      delete expiryTimers[workflowPermanentId];
      const fresh = getAttempts(workflowPermanentId);
      setAttempts(workflowPermanentId, fresh);
    }, delay + 100);

    // No cleanup: the timer must survive component unmounts (e.g. a NodeHeader
    // being deleted) so auto-recovery still fires for remaining instances.
  }, [attempts, workflowPermanentId, setAttempts]);

  const isRateLimited = attempts.length >= MAX_ATTEMPTS;

  const recordAttempt = useCallback(() => {
    if (!workflowPermanentId) return;
    const current = getAttempts(workflowPermanentId);
    const updated = [...current, Date.now()].slice(-MAX_ATTEMPTS);
    saveAttempts(workflowPermanentId, updated);
    setAttempts(workflowPermanentId, updated);
  }, [workflowPermanentId, setAttempts]);

  const resetOnSuccess = useCallback(() => {
    if (!workflowPermanentId) return;
    // Guard: skip if already cleared (avoids redundant localStorage writes
    // when multiple success paths call this, e.g. cycleBrowser.onSuccess +
    // the dedicated useEffect that watches debugSession.browser_session_id).
    const current = getAttempts(workflowPermanentId);
    if (current.length === 0) return;
    saveAttempts(workflowPermanentId, []);
    setAttempts(workflowPermanentId, []);
    // Clear the expiry timer so a new one can be scheduled if the user
    // gets rate-limited again (e.g. after clicking "Try again").
    if (expiryTimers[workflowPermanentId]) {
      clearTimeout(expiryTimers[workflowPermanentId]);
      delete expiryTimers[workflowPermanentId];
    }
  }, [workflowPermanentId, setAttempts]);

  return {
    isRateLimited,
    recordAttempt,
    resetOnSuccess,
  };
}

export { useBrowserSessionRateLimit };
