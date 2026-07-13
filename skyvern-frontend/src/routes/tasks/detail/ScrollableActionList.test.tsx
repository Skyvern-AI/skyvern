// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("@/api/AxiosClient", () => ({
  getClient: async () => ({
    get: async () => ({ data: null }),
  }),
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => undefined,
}));

import { Action, ActionTypes } from "@/api/types";
import { ScrollableActionList } from "./ScrollableActionList";

function buildAction(overrides: Partial<Action> = {}): Action {
  return {
    reasoning: "Click the submit button",
    confidence: 0.9,
    type: ActionTypes.Click,
    input: "",
    success: true,
    stepId: "step_1",
    index: 0,
    created_by: null,
    screenshotArtifactId: null,
    ...overrides,
  };
}

function renderList() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ScrollableActionList
        data={[buildAction()]}
        activeIndex={0}
        onActiveIndexChange={() => {}}
        showStreamOption={false}
        taskDetails={{ actions: 1, steps: 1 }}
      />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
});

describe("ScrollableActionList", () => {
  it("renders the action card with its reasoning", () => {
    renderList();

    // getByText throws if not found, so reaching this line means it rendered
    screen.getByText("Click the submit button");
  });
});
