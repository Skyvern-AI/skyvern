// @vitest-environment jsdom

vi.mock("@/api/AxiosClient", () => ({ getClient: vi.fn() }));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));
vi.mock("./StepArtifacts", () => ({
  StepArtifacts: ({
    id,
    stepProps,
  }: {
    id: string;
    stepProps: StepApiResponse;
  }) => (
    <div data-testid="active-step">
      {id}:{stepProps.order}
    </div>
  ),
}));
vi.mock("./StepNavigation", () => ({
  StepNavigation: ({ activeIndex }: { activeIndex: number }) => (
    <div data-testid="active-step-index">{activeIndex}</div>
  ),
}));

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { getClient } from "@/api/AxiosClient";
import { Status, type StepApiResponse } from "@/api/types";
import { StepArtifactsLayout } from "./StepArtifactsLayout";

const mockedGetClient = vi.mocked(getClient);

function buildStep(overrides: Partial<StepApiResponse> = {}): StepApiResponse {
  return {
    step_id: "stp_0",
    task_id: "tsk_123",
    created_at: "2026-01-01T00:00:00Z",
    modified_at: "2026-01-01T00:00:00Z",
    input_token_count: 0,
    is_last: false,
    order: 0,
    organization_id: "org_123",
    retry_index: 0,
    status: Status.Completed,
    step_cost: 0,
    ...overrides,
  };
}

function renderLayout(initialEntry: string, steps: Array<StepApiResponse>) {
  mockedGetClient.mockResolvedValue({
    get: vi.fn().mockResolvedValue({ data: steps }),
  } as never);

  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route
            path="/tasks/:taskId/diagnostics"
            element={<StepArtifactsLayout />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("StepArtifactsLayout", () => {
  it("selects the diagnostic step by step_id when present", async () => {
    renderLayout("/tasks/tsk_123/diagnostics?step_id=stp_2", [
      buildStep({ step_id: "stp_1", order: 0 }),
      buildStep({ step_id: "stp_2", order: 1 }),
    ]);

    expect((await screen.findByTestId("active-step")).textContent).toBe(
      "stp_2:1",
    );
    expect(screen.getByTestId("active-step-index").textContent).toBe("1");
  });

  it("falls back to the first step instead of rendering blank diagnostics for an out-of-range step", async () => {
    renderLayout("/tasks/tsk_123/diagnostics?step=99", [
      buildStep({ step_id: "stp_1", order: 0 }),
      buildStep({ step_id: "stp_2", order: 1 }),
    ]);

    expect((await screen.findByTestId("active-step")).textContent).toBe(
      "stp_1:0",
    );
    expect(screen.getByTestId("active-step-index").textContent).toBe("0");
  });

  it("uses the step index fallback when step_id does not resolve for a retried step", async () => {
    renderLayout("/tasks/tsk_123/diagnostics?step_id=stp_stale&step=2", [
      buildStep({ step_id: "stp_order_0", order: 0 }),
      buildStep({
        step_id: "stp_order_1_original",
        order: 1,
        retry_index: 0,
      }),
      buildStep({
        step_id: "stp_order_1_retry",
        order: 1,
        retry_index: 1,
      }),
    ]);

    expect((await screen.findByTestId("active-step")).textContent).toBe(
      "stp_order_1_retry:1",
    );
    expect(screen.getByTestId("active-step-index").textContent).toBe("2");
  });
});
