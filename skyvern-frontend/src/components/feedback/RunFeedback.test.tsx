// @vitest-environment jsdom
import { type ReactNode } from "react";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RunFeedback } from "./RunFeedback";

const { getMock, postMock } = vi.hoisted(() => ({
  getMock: vi.fn(),
  postMock: vi.fn(),
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => vi.fn(),
}));

vi.mock("@/hooks/useUser", () => ({
  useUser: () => ({
    get: () => ({ id: "u_1", email: "me@example.com", name: "Me" }),
  }),
}));

vi.mock("@/api/AxiosClient", () => ({
  // The route is on base_router (/v1); the default /api/v1 client would 404.
  getClient: vi.fn(async (_credentialGetter: unknown, version?: string) => {
    if (version !== "sans-api-v1") {
      throw new Error(`expected the sans-api-v1 client, got ${version}`);
    }
    return { get: getMock, post: postMock };
  }),
}));

function wrapper({ children }: { children: ReactNode }) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

function savedRow(overrides: Record<string, unknown> = {}) {
  return {
    run_feedback_id: "fb_1",
    organization_id: "o_1",
    target_type: "workflow_run",
    target_id: "wr_1",
    context_id: "wpid_1",
    rating: "down",
    reason: null,
    needs_support: false,
    submitted_by: "me@example.com",
    created_at: "2026-09-22T00:00:00Z",
    modified_at: "2026-09-22T00:00:00Z",
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("RunFeedback", () => {
  it("thumbs down saves immediately, then Send posts the reason and the help flag", async () => {
    getMock.mockResolvedValue({ data: null });
    postMock.mockImplementation(
      async (
        _url: string,
        body: { reason?: string | null; needs_support?: boolean },
      ) => ({
        data: savedRow({
          reason: body.reason ?? null,
          needs_support: body.needs_support ?? false,
        }),
      }),
    );

    render(<RunFeedback targetType="workflow_run" targetId="wr_1" />, {
      wrapper,
    });
    await screen.findByText("Did this run do what you wanted?");

    fireEvent.click(screen.getByRole("button", { name: "Thumbs down" }));

    await waitFor(() => expect(postMock).toHaveBeenCalledTimes(1));
    expect(postMock.mock.calls[0]?.[1]).toMatchObject({
      target_type: "workflow_run",
      target_id: "wr_1",
      rating: "down",
      needs_support: false,
      submitted_by: "me@example.com",
    });

    fireEvent.change(await screen.findByLabelText("Feedback reason"), {
      target: { value: "Stuck on login" },
    });
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(postMock).toHaveBeenCalledTimes(2));
    expect(postMock.mock.calls[1]?.[1]).toMatchObject({
      rating: "down",
      reason: "Stuck on login",
      needs_support: true,
    });
    await screen.findByText("Thanks, that helps.");
    expect(screen.queryByLabelText("Feedback reason")).toBeNull();
  });

  it("re-tapping the saved thumb clears the rating", async () => {
    getMock.mockResolvedValue({ data: savedRow({ rating: "up" }) });
    postMock.mockResolvedValue({ data: null });

    render(<RunFeedback targetType="task" targetId="tsk_1" />, { wrapper });
    await waitFor(() =>
      expect(
        screen
          .getByRole("button", { name: "Thumbs up" })
          .getAttribute("aria-pressed"),
      ).toBe("true"),
    );

    fireEvent.click(screen.getByRole("button", { name: "Thumbs up" }));

    await waitFor(() => expect(postMock).toHaveBeenCalledTimes(1));
    expect(postMock.mock.calls[0]?.[1]).toMatchObject({
      target_type: "task",
      target_id: "tsk_1",
      rating: null,
    });
    await screen.findByText("Did this run do what you wanted?");
  });

  it("report variant offers one action that saves a thumbs down and opens the details panel", async () => {
    getMock.mockResolvedValue({ data: null });
    postMock.mockImplementation(
      async (_url: string, body: { rating: string | null }) => ({
        data: body.rating ? savedRow({ target_id: "wr_failed" }) : null,
      }),
    );

    render(
      <RunFeedback
        targetType="workflow_run"
        targetId="wr_failed"
        variant="report"
      />,
      { wrapper },
    );
    await waitFor(() => expect(getMock).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: "Thumbs up" })).toBeNull();
    expect(screen.queryByText("Did this run do what you wanted?")).toBeNull();

    fireEvent.click(
      screen.getByRole("button", { name: "Report this failure" }),
    );

    await waitFor(() => expect(postMock).toHaveBeenCalledTimes(1));
    expect(postMock.mock.calls[0]?.[1]).toMatchObject({
      target_id: "wr_failed",
      rating: "down",
    });
    await screen.findByText("Reported.");
    await screen.findByLabelText("Feedback reason");
    expect(screen.getByRole("checkbox")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(postMock).toHaveBeenCalledTimes(2));
    expect(postMock.mock.calls[1]?.[1]).toMatchObject({ rating: null });
    await screen.findByRole("button", { name: "Report this failure" });
  });
});
