// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from "vitest";

import { describeCopiedCommand } from "./apiCommands";

const { expiresAtMock } = vi.hoisted(() => ({ expiresAtMock: vi.fn() }));

vi.mock("@/util/env", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/util/env")>()),
  getRuntimeApiKeyExpiresAt: expiresAtMock,
}));

describe("describeCopiedCommand", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("warns that a copied command carries a short-lived session token", () => {
    // Far-future epoch seconds, so the assertion never depends on the wall clock.
    expiresAtMock.mockReturnValue(1_893_456_789);

    const description = describeCopiedCommand("cURL");

    expect(description).toMatch(/short-lived session token/i);
    expect(description).toMatch(/organization API key/i);
    expect(description).toMatch(/cURL/);
  });

  it("omits the warning when the credential is a durable key with no expiry", () => {
    expiresAtMock.mockReturnValue(null);

    expect(describeCopiedCommand("PowerShell")).toBe(
      "The PowerShell command has been copied to your clipboard.",
    );
  });
});
