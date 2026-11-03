// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
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
    summary: {
      body: { text: "Click the submit button", isProse: true },
      outcome: null,
    },
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

function renderList(action = buildAction()) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ScrollableActionList
        data={[action]}
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

  it("renders a paste text action label", () => {
    renderList(buildAction({ type: ActionTypes.PasteText }));

    screen.getByText("Paste Text");
  });

  // A goto_url row's only text is its intention or its outcome, and an input_text row's is the
  // value typed: neither is markdown, and a value containing "*" is not emphasis.
  it("renders a summary that is not the model's prose verbatim", () => {
    const { container } = renderList(
      buildAction({
        summary: {
          body: {
            text: "https://example.com/a*b*c (HTTP 404, dead end)",
            isProse: false,
          },
          outcome: null,
        },
      }),
    );

    screen.getByText("https://example.com/a*b*c (HTTP 404, dead end)");
    expect(container.querySelector("em")).toBeNull();
  });

  // The card that showed only the plan read a dead end as a completed navigation.
  it("shows the recorded outcome next to what the action set out to do", () => {
    renderList(
      buildAction({
        type: ActionTypes.GotoUrl,
        summary: {
          body: {
            text: "Tried to navigate to https://example.com/contact-us/",
            isProse: true,
          },
          outcome: "https://example.com/contact-us/ (HTTP 404, dead end)",
        },
      }),
    );

    screen.getByText(
      /Tried to navigate to https:\/\/example\.com\/contact-us\//,
    );
    screen.getByText(/HTTP 404, dead end/);
  });

  // A provider summary arrives as multiple paragraphs; collapsing them would run the heading into
  // the body it introduces.
  it("keeps a multi-paragraph reasoning in separate blocks", () => {
    const { container } = renderList(
      buildAction({
        summary: {
          body: {
            text: "**Investigating the iframe**\n\nThen reading the table",
            isProse: true,
          },
          outcome: null,
        },
      }),
    );

    expect(container.querySelectorAll("span.block").length).toBe(2);
  });
});
