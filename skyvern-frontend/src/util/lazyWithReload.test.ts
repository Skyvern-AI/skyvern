import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import {
  installChunkLoadErrorHandler,
  isChunkLoadError,
} from "./lazyWithReload";

const RELOAD_GUARD_KEY = "skyvern.chunkReloadAt";

function dispatchUnhandledRejection(reason: unknown): void {
  const event = new Event("unhandledrejection", {
    cancelable: true,
  }) as Event & { reason: unknown };
  event.reason = reason;
  window.dispatchEvent(event);
}

describe("isChunkLoadError", () => {
  it("recognizes a webpack/Clerk ChunkLoadError string", () => {
    expect(isChunkLoadError("ChunkLoadError: Loading chunk 344 failed")).toBe(
      true,
    );
  });

  it("recognizes a ChunkLoadError instance by its message", () => {
    const error = new Error("Loading chunk 344 failed.");
    error.name = "ChunkLoadError";
    expect(isChunkLoadError(error)).toBe(true);
  });

  it("recognizes a named-chunk failure", () => {
    expect(
      isChunkLoadError("Loading chunk vendors-node_modules_clerk failed"),
    ).toBe(true);
  });

  it("still recognizes Vite dynamic-import failures", () => {
    expect(
      isChunkLoadError(
        new Error("Failed to fetch dynamically imported module: /assets/x.js"),
      ),
    ).toBe(true);
  });

  it("ignores unrelated errors", () => {
    expect(
      isChunkLoadError(new Error("Cannot read properties of undefined")),
    ).toBe(false);
    expect(isChunkLoadError(null)).toBe(false);
    expect(isChunkLoadError(undefined)).toBe(false);
  });
});

describe("installChunkLoadErrorHandler", () => {
  const reloadMock = vi.fn();

  beforeAll(() => {
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { reload: reloadMock },
    });
    installChunkLoadErrorHandler();
  });

  beforeEach(() => {
    reloadMock.mockClear();
    sessionStorage.removeItem(RELOAD_GUARD_KEY);
  });

  it("reloads once on a ChunkLoadError unhandledrejection", () => {
    dispatchUnhandledRejection("ChunkLoadError: Loading chunk 344 failed");
    expect(reloadMock).toHaveBeenCalledTimes(1);
  });

  it("does not reload again within the guard window", () => {
    dispatchUnhandledRejection("ChunkLoadError: Loading chunk 344 failed");
    dispatchUnhandledRejection("ChunkLoadError: Loading chunk 344 failed");
    expect(reloadMock).toHaveBeenCalledTimes(1);
  });

  it("ignores unrelated unhandledrejections", () => {
    dispatchUnhandledRejection(new Error("some unrelated rejection"));
    expect(reloadMock).not.toHaveBeenCalled();
  });
});
