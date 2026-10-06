// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

const { getMock } = vi.hoisted(() => ({ getMock: vi.fn() }));

vi.mock("@/api/AxiosClient", () => ({
  getClient: () =>
    Promise.resolve({ get: getMock, post: vi.fn(), delete: vi.fn() }),
}));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));
vi.mock("posthog-js/react", () => ({
  useFeatureFlagEnabled: () => false,
  useFeatureFlagVariantKey: () => undefined,
  usePostHog: () => undefined,
}));

import { FeatureFlagContext } from "@/hooks/useFeatureFlag";
import { WORKFLOW_TAGGING_FLAG } from "@/util/featureFlags";
import type { WorkflowApiResponse } from "./types/workflowTypes";
import { WorkflowsFlat } from "./WorkflowsFlat";

class MockResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
(globalThis as { ResizeObserver: unknown }).ResizeObserver = MockResizeObserver;

afterEach(() => {
  cleanup();
  getMock.mockReset();
});

// The agent list is the only `/workflows` request with `only_workflows=true`.
function agentListRequests(): Array<string> {
  return getMock.mock.calls
    .filter(([url]) => url === "/workflows")
    .map(([, config]) => new URLSearchParams(config?.params ?? {}))
    .filter((params) => params.get("only_workflows") === "true")
    .map((params) => params.toString());
}

it("holds a ?tags= link while the tagging flag is pending, then requests it filtered", async () => {
  getMock.mockResolvedValue({ data: [] });
  const tagging = { current: undefined as boolean | undefined };
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const tree = () => (
    <QueryClientProvider client={client}>
      <FeatureFlagContext.Provider
        value={(flag) =>
          flag === WORKFLOW_TAGGING_FLAG ? tagging.current : undefined
        }
      >
        <MemoryRouter initialEntries={["/agents?tags=env:prod"]}>
          <WorkflowsFlat />
        </MemoryRouter>
      </FeatureFlagContext.Provider>
    </QueryClientProvider>
  );

  const view = render(tree());
  // Other queries on the page (folders, imports) settle; the agent list must not go out unfiltered.
  await waitFor(() => expect(getMock).toHaveBeenCalled());
  await waitFor(() => expect(client.isFetching()).toBe(0));
  expect(agentListRequests()).toEqual([]);

  tagging.current = true;
  view.rerender(tree());

  await waitFor(() => expect(agentListRequests()).not.toEqual([]));
  expect(agentListRequests().every((query) => query.includes("tags=env"))).toBe(
    true,
  );
});

it("does not carry unfiltered rows into a held ?tags= link", async () => {
  const unfiltered = {
    workflow_permanent_id: "wpid_1",
    workflow_id: "w_1",
    title: "Unfiltered agent",
    is_template: false,
    folder_id: null,
    created_at: "2026-10-01T00:00:00Z",
    modified_at: "2026-10-01T00:00:00Z",
    workflow_definition: { parameters: [], blocks: [] },
  } as unknown as WorkflowApiResponse;
  getMock.mockImplementation((url: string, config?: { params?: unknown }) => {
    const params = new URLSearchParams(
      (config?.params ?? {}) as Record<string, string>,
    );
    const isAgentList =
      url === "/workflows" && params.get("only_workflows") === "true";
    return Promise.resolve({ data: isAgentList ? [unfiltered] : [] });
  });
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  let navigate: (to: string) => void = () => {};
  function Navigator() {
    navigate = useNavigate();
    return null;
  }

  render(
    <QueryClientProvider client={client}>
      <FeatureFlagContext.Provider value={() => undefined}>
        <MemoryRouter initialEntries={["/agents"]}>
          <Navigator />
          <WorkflowsFlat />
        </MemoryRouter>
      </FeatureFlagContext.Provider>
    </QueryClientProvider>,
  );
  expect(await screen.findByText("Unfiltered agent")).toBeTruthy();

  act(() => navigate("/agents?tags=env:prod"));

  expect(screen.queryByText("Unfiltered agent")).toBeNull();
  expect(agentListRequests()).toEqual([
    "page=1&page_size=10&only_workflows=true",
  ]);
});
