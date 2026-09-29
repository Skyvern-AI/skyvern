// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import type { WorkflowVersion } from "../../hooks/useWorkflowVersionsQuery";

// The review contract under test lives in the header, so the canvas stack is
// stubbed out; the diff still runs on the real chain helpers.
vi.mock("../FlowRenderer", () => ({
  FlowRenderer: () => <div data-testid="flow" />,
}));
vi.mock("../workflowEditorUtils", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../workflowEditorUtils")>()),
  getElements: () => ({ nodes: [], edges: [] }),
}));
vi.mock("../nodes", () => ({}));

import { WorkflowComparisonPanel } from "./WorkflowComparisonPanel";

const version = (title: string, blocks: unknown[] = []) =>
  ({
    title,
    workflow_definition: { version: 1, parameters: [], blocks },
  }) as unknown as WorkflowVersion;

afterEach(() => {
  cleanup();
});

describe("WorkflowComparisonPanel diff summary", () => {
  test("a first proposal against an empty Current reads as a new workflow", () => {
    const block = {
      label: "open_target",
      block_type: "goto_url",
      url: "https://example.test/",
    };
    render(
      <WorkflowComparisonPanel
        version1={version("Current")}
        version2={version("Copilot Suggestion", [block])}
        mode="copilot"
        onCopilotReviewClose={vi.fn()}
      />,
    );

    expect(screen.getByText("New workflow")).toBeTruthy();
    expect(screen.getByText("1 block")).toBeTruthy();
    expect(screen.getByText(/This is a new workflow/)).toBeTruthy();
    expect(screen.queryByText(/unchanged/)).toBeNull();
  });
});

describe("WorkflowComparisonPanel copilot review actions", () => {
  test("a second click while Accept is settling does not settle again", async () => {
    let finish: () => void = () => {};
    const onCopilotReviewClose = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          finish = resolve;
        }),
    );

    render(
      <WorkflowComparisonPanel
        version1={version("Current")}
        version2={version("Copilot Suggestion")}
        mode="copilot"
        onCopilotReviewClose={onCopilotReviewClose}
      />,
    );

    const accept = screen.getByRole("button", { name: "Accept changes" });
    const reject = screen.getByRole("button", { name: "Reject" });

    await act(async () => {
      fireEvent.click(accept);
      fireEvent.click(accept);
      fireEvent.click(reject);
      fireEvent.keyDown(window, { key: "Escape" });
    });

    expect(onCopilotReviewClose).toHaveBeenCalledTimes(1);
    expect(onCopilotReviewClose).toHaveBeenCalledWith("approve");
    expect(accept).toHaveProperty("disabled", true);
    expect(reject).toHaveProperty("disabled", true);

    await act(async () => {
      finish();
    });

    expect(accept).toHaveProperty("disabled", false);
    expect(reject).toHaveProperty("disabled", false);
  });
});
