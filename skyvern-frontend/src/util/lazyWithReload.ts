import React from "react";

const CHUNK_LOAD_PATTERNS = [
  /Failed to fetch dynamically imported module/i,
  /Importing a module script failed/i,
  /error loading dynamically imported module/i,
];

const RELOAD_GUARD_KEY = "skyvern.chunkReloadAt";
const RELOAD_GUARD_WINDOW_MS = 10_000;
// Fallback when sessionStorage is unavailable (e.g. private mode or sandboxed
// iframes that throw on access). Prevents a reload-storm within one session.
let inMemoryReloadFiredAt = 0;

function isChunkLoadError(error: unknown): boolean {
  if (!error) return false;
  const message =
    error instanceof Error
      ? error.message
      : typeof error === "string"
        ? error
        : (error as { message?: string })?.message;
  if (!message) return false;
  return CHUNK_LOAD_PATTERNS.some((pattern) => pattern.test(message));
}

function reloadOnce(): void {
  const now = Date.now();
  let storageOk = false;
  try {
    const last = Number(sessionStorage.getItem(RELOAD_GUARD_KEY) ?? "0");
    if (now - last < RELOAD_GUARD_WINDOW_MS) return;
    sessionStorage.setItem(RELOAD_GUARD_KEY, String(now));
    storageOk = true;
  } catch {
    // sessionStorage may be unavailable (private mode, sandboxed iframe).
  }
  if (!storageOk) {
    if (now - inMemoryReloadFiredAt < RELOAD_GUARD_WINDOW_MS) return;
    inMemoryReloadFiredAt = now;
  }
  window.location.reload();
}

export async function importWithRetry<T>(
  factory: () => Promise<T>,
): Promise<T> {
  try {
    return await factory();
  } catch (err) {
    if (!isChunkLoadError(err)) throw err;
    try {
      return await factory();
    } catch (retryErr) {
      if (isChunkLoadError(retryErr)) {
        reloadOnce();
        // Freeze the loading state; the imminent page reload will resolve this.
        return new Promise<T>(() => {});
      }
      throw retryErr;
    }
  }
}

export function lazyWithReload<T extends React.ComponentType<unknown>>(
  factory: () => Promise<{ default: T }>,
): React.LazyExoticComponent<T> {
  return React.lazy(() => importWithRetry(factory));
}

export function installChunkLoadErrorHandler(): void {
  if (typeof window === "undefined") return;

  // Vite emits this when an esmodule preload fails (typically a stale chunk
  // hash after a deploy). It is not part of the standard WindowEventMap.
  window.addEventListener(
    "vite:preloadError" as keyof WindowEventMap,
    ((event: Event) => {
      event.preventDefault();
      reloadOnce();
    }) as EventListener,
  );

  window.addEventListener("unhandledrejection", (event) => {
    if (isChunkLoadError(event.reason)) {
      event.preventDefault();
      reloadOnce();
    }
  });
}
