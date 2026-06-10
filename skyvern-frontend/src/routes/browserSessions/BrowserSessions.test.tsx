// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import { BrowserSessions } from "./BrowserSessions";
import { type BrowserSession } from "@/routes/workflows/types/browserSessionTypes";

const openUnoccupiedSession: BrowserSession = {
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

const createBrowserSessionMock = vi.fn();

vi.mock("@/routes/browserSessions/hooks/useBrowserSessionsQuery", () => ({
  useBrowserSessionsQuery: vi.fn((page: number) => ({
    data: page === 1 ? [openUnoccupiedSession] : [],
    isLoading: false,
  })),
}));

vi.mock(
  "@/routes/browserSessions/hooks/useCreateBrowserSessionMutation",
  () => ({
    useCreateBrowserSessionMutation: vi.fn(() => ({
      isPending: false,
      mutate: createBrowserSessionMock,
    })),
  }),
);

afterEach(() => {
  cleanup();
  createBrowserSessionMock.mockClear();
});

function renderPage() {
  render(
    <MemoryRouter
      initialEntries={["/browser-sessions"]}
      future={{ v7_relativeSplatPath: true, v7_startTransition: true }}
    >
      <BrowserSessions />
    </MemoryRouter>,
  );
}

describe("BrowserSessions", () => {
  it("renders a session row with its id and timeout", () => {
    renderPage();
    expect(screen.getByText("session-1")).toBeTruthy();
    expect(screen.getByText("60m")).toBeTruthy();
  });

  it("derives Open=Yes / Occupied=No from the session's lifecycle fields", () => {
    renderPage();
    // completed_at === null && started_at !== null => open
    expect(screen.getAllByText("Yes")).toHaveLength(1);
    // runnable_id === null => not occupied
    expect(screen.getAllByText("No")).toHaveLength(1);
  });

  it("marks Captcha Solver as enterprise without changing the extension value", () => {
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    const captchaSolverLabel = screen.getByText("Captcha Solver");
    expect(captchaSolverLabel.parentElement?.textContent).toContain(
      "Enterprise",
    );

    fireEvent.click(screen.getByLabelText(/Captcha Solver/));
    const createButtons = screen.getAllByRole("button", { name: "Create" });
    fireEvent.click(createButtons[createButtons.length - 1]!);

    expect(createBrowserSessionMock).toHaveBeenCalledWith({
      proxyLocation: "RESIDENTIAL",
      timeout: 60,
      browserType: null,
      extensions: ["captcha-solver"],
    });
  });
});
