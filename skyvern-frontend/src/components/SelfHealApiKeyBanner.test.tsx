// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const { diagnosticsState } = vi.hoisted(() => ({
  diagnosticsState: {
    data: { status: "missing_api_key" as const },
    error: null,
    isLoading: false,
    refetch: vi.fn(),
  },
}));

vi.mock("@/hooks/useAuthDiagnostics", () => ({
  useAuthDiagnostics: () => diagnosticsState,
}));

import { SelfHealApiKeyBanner } from "./SelfHealApiKeyBanner";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("SelfHealApiKeyBanner", () => {
  it("directs the operator to the CLI without offering browser repair", () => {
    render(<SelfHealApiKeyBanner />);

    screen.getByText("skyvern doctor --fix");
    expect(
      screen.queryByRole("button", { name: "Regenerate API key" }),
    ).toBeNull();
  });
});
