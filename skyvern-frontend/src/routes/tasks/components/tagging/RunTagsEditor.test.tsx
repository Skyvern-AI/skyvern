// @vitest-environment jsdom

import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({
  taggingEnabled: true,
  mutate: vi.fn(),
}));

vi.mock("@/hooks/useFeatureFlag", () => ({
  useFeatureFlag: () => state.taggingEnabled,
}));

vi.mock("../../hooks/useRunTagsQuery", () => ({
  useRunTagsQuery: () => ({
    data: [
      { key: "env", value: "prod" },
      { key: "skyvern.platform", value: "browser" },
    ],
  }),
}));

vi.mock("../../hooks/useRunTagSuggestionsQuery", () => ({
  useRunTagSuggestionsQuery: () => ({
    data: {
      keys: ["env", "skyvern.platform"],
      valuesByKey: new Map([
        ["env", ["prod", "staging"]],
        ["skyvern.platform", ["browser"]],
      ]),
      labels: ["urgent"],
    },
  }),
}));

vi.mock("../../hooks/useRunTagMutations", () => ({
  useApplyRunTagsMutation: () => ({
    mutate: state.mutate,
    isPending: false,
  }),
}));

vi.mock("@/routes/workflows/hooks/useTagKeysQuery", () => ({
  useTagKeysQuery: () => ({
    data: [{ key: "env", description: "Environment", workflow_count: 1 }],
  }),
}));

vi.mock("@/routes/workflows/hooks/useTagValuesQuery", () => ({
  useTagValuesQuery: () => ({ data: new Map() }),
}));

import { RunTagsEditor } from "./RunTagsEditor";

class MockResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

vi.stubGlobal("ResizeObserver", MockResizeObserver);
Element.prototype.scrollIntoView = () => {};

afterEach(() => {
  state.taggingEnabled = true;
  vi.clearAllMocks();
});

describe("RunTagsEditor", () => {
  it("shows system tags without removal and removes user tags from their chips", () => {
    render(<RunTagsEditor workflowRunId="wr_1" />);

    expect(screen.getByText("browser")).toBeTruthy();
    expect(
      screen.queryByRole("button", {
        name: "Remove skyvern.platform: browser",
      }),
    ).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Remove env: prod" }));

    expect(state.mutate).toHaveBeenCalledWith({
      workflowRunId: "wr_1",
      data: { tags_to_delete: [{ key: "env" }] },
    });
  });

  it("uses run suggestions for new tags", () => {
    render(<RunTagsEditor workflowRunId="wr_1" />);

    fireEvent.click(screen.getByRole("button", { name: "Manage tags" }));
    fireEvent.change(screen.getByPlaceholderText(/label or group/i), {
      target: { value: "urgent" },
    });
    fireEvent.click(screen.getByRole("option", { name: /Add urgent/i }));

    expect(state.mutate).toHaveBeenCalledWith(
      {
        workflowRunId: "wr_1",
        data: { tags: [{ key: null, value: "urgent" }] },
      },
      expect.any(Object),
    );
  });

  it("renders nothing when workflow tagging is disabled", () => {
    state.taggingEnabled = false;

    const { container } = render(<RunTagsEditor workflowRunId="wr_1" />);

    expect(container.textContent).toBe("");
  });
});
