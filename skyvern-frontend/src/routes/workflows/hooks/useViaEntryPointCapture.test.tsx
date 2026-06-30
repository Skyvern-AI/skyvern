// @vitest-environment jsdom

import { renderHook } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useViaEntryPointCapture } from "./useViaEntryPointCapture";

const { capture } = vi.hoisted(() => ({ capture: vi.fn() }));

vi.mock("posthog-js/react", () => ({
  usePostHog: () => ({ capture }),
}));

function wrapperFor(initialEntry: string) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <MemoryRouter initialEntries={[initialEntry]}>{children}</MemoryRouter>
    );
  };
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("useViaEntryPointCapture", () => {
  it("fires copilot.discover.started with the via entry point on mount", () => {
    renderHook(() => useViaEntryPointCapture(), {
      wrapper: wrapperFor("/workflows/wpid_1/build?via=discover"),
    });

    expect(capture).toHaveBeenCalledTimes(1);
    expect(capture).toHaveBeenCalledWith("copilot.discover.started", {
      entry_point: "discover",
    });
  });

  it("does not fire when no via param is present", () => {
    renderHook(() => useViaEntryPointCapture(), {
      wrapper: wrapperFor("/workflows/wpid_1/build"),
    });

    expect(capture).not.toHaveBeenCalled();
  });
});
