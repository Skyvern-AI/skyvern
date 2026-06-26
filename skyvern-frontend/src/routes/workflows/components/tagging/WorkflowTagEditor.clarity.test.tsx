// @vitest-environment jsdom
import {
  afterAll,
  afterEach,
  beforeAll,
  describe,
  expect,
  it,
  vi,
} from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { WorkflowTagEditor } from "./WorkflowTagEditor";

// cmdk needs ResizeObserver and scrollIntoView, which jsdom lacks. Install them
// for this suite only and restore afterward so they don't leak into other test
// files sharing the Vitest process.
class MockResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

const originalScrollIntoView = Element.prototype.scrollIntoView;

beforeAll(() => {
  vi.stubGlobal("ResizeObserver", MockResizeObserver);
  Element.prototype.scrollIntoView = () => {};
});

afterAll(() => {
  vi.unstubAllGlobals();
  if (originalScrollIntoView) {
    Element.prototype.scrollIntoView = originalScrollIntoView;
  } else {
    delete (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView;
  }
});

vi.mock("../../hooks/useWorkflowTagMutations", () => ({
  useApplyWorkflowTagsMutation: () => ({ mutate: vi.fn(), isPending: false }),
}));

afterEach(cleanup);

function renderEditor() {
  return render(
    <WorkflowTagEditor workflowPermanentId="wpid_1" tags={[]} tagKeys={[]} />,
  );
}

function openAndType(query: string) {
  fireEvent.click(screen.getByRole("button", { name: /edit tags/i }));
  const input = screen.getByPlaceholderText(/add a tag/i);
  fireEvent.change(input, { target: { value: query } });
}

describe("WorkflowTagEditor group-vs-value clarity", () => {
  it("shows a bare entry as a value-only label: empty group slot, no color", () => {
    renderEditor();
    openAndType("prod");
    // The group half renders the empty "no group" slot...
    expect(screen.getByText("no group")).toBeTruthy();
    // ...and the typed value fills the label half.
    expect(screen.getAllByText("prod").length).toBeGreaterThan(0);
    // Color is grouped-only, so a value-only label exposes no swatches.
    expect(screen.queryByRole("button", { name: "blue" })).toBeNull();
  });

  it("shows a key:value entry as a grouped label: filled group + color", () => {
    renderEditor();
    openAndType("env:prod");
    // No empty group slot — the group half is filled with the key.
    expect(screen.queryByText("no group")).toBeNull();
    expect(screen.getAllByText("env").length).toBeGreaterThan(0);
    expect(screen.getAllByText("prod").length).toBeGreaterThan(0);
    // Grouped labels expose the color picker.
    expect(screen.getByRole("button", { name: "blue" })).toBeTruthy();
  });

  it("treats a group prefix as grouped before the value is typed", () => {
    renderEditor();
    openAndType("env:");
    // Group half is already filled; the label half prompts for the value.
    expect(screen.getAllByText("env").length).toBeGreaterThan(0);
    expect(screen.getByText("type a label")).toBeTruthy();
    expect(screen.queryByText("no group")).toBeNull();
    // Already in group mode, so the color picker is available.
    expect(screen.getByRole("button", { name: "blue" })).toBeTruthy();
  });
});
