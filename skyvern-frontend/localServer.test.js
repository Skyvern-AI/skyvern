import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("http", () => {
  const createServer = vi.fn(() => ({ listen: vi.fn() }));
  return { createServer, default: { createServer } };
});
vi.mock("serve-handler", () => ({ default: vi.fn() }));

describe("openBrowserSafely", () => {
  afterEach(() => {
    vi.resetModules();
    vi.doUnmock("open");
  });

  it("does not throw when opening a browser fails (e.g. headless Docker container)", async () => {
    vi.doMock("open", () => ({
      default: vi.fn().mockRejectedValue(new Error("spawn xdg-open ENOENT")),
    }));

    const { openBrowserSafely } = await import("./localServer.js");

    await expect(
      openBrowserSafely("http://localhost:8080"),
    ).resolves.toBeUndefined();
  });

  it("still opens the browser when it succeeds", async () => {
    const open = vi.fn().mockResolvedValue(undefined);
    vi.doMock("open", () => ({ default: open }));

    const { openBrowserSafely } = await import("./localServer.js");

    await openBrowserSafely("http://localhost:8080");

    expect(open).toHaveBeenCalledWith("http://localhost:8080");
  });
});
