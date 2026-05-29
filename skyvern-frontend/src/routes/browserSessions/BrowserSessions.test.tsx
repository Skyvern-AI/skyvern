// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import { BrowserSessions } from "./BrowserSessions";
import { type BrowserSession } from "@/routes/workflows/types/browserSessionTypes";

const openBrowserSession: BrowserSession = {
  browser_address: "ws://example.test/devtools/browser/session-1",
  browser_session_id: "session-1",
  completed_at: null,
  downloaded_files: null,
  recordings: [],
  runnable_id: null,
  runnable_type: null,
  started_at: "2026-05-28T00:00:00.000Z",
  status: "running",
  timeout: 60,
  vnc_streaming_supported: true,
};

vi.mock("@/routes/browserSessions/hooks/useBrowserSessionsQuery", () => ({
  useBrowserSessionsQuery: vi.fn((page: number) => ({
    data: page === 1 ? [openBrowserSession] : [],
    isLoading: false,
  })),
}));

vi.mock(
  "@/routes/browserSessions/hooks/useCreateBrowserSessionMutation",
  () => ({
    useCreateBrowserSessionMutation: vi.fn(() => ({
      isPending: false,
      mutate: vi.fn(),
    })),
  }),
);

afterEach(() => {
  cleanup();
});

describe("BrowserSessions", () => {
  it("renders the open session Yes pill with completed run status colors", () => {
    render(
      <MemoryRouter
        initialEntries={["/browser-sessions"]}
        future={{ v7_relativeSplatPath: true, v7_startTransition: true }}
      >
        <BrowserSessions />
      </MemoryRouter>,
    );

    const openPill = screen.getByText("Yes");

    const pillClasses = openPill.className.split(/\s+/);

    expect(pillClasses).toContain("border-green-900/20");
    expect(pillClasses).toContain("bg-green-900/10");
    expect(pillClasses).toContain("text-green-800");
    expect(pillClasses).toContain("hover:bg-green-900/15");
    expect(pillClasses).toContain("dark:bg-green-900");
    expect(pillClasses).toContain("dark:text-green-50");
    expect(openPill.className).not.toMatch(/emerald|bg-success/);
    expect(openPill.className).not.toMatch(/shadow-(emerald|\[)/);
  });
});
