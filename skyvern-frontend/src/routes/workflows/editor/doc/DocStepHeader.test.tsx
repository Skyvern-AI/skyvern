// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ReactFlowProvider } from "@xyflow/react";
import { MemoryRouter } from "react-router-dom";

import type { WorkflowBlock } from "../../types/workflowTypes";
import { getElements } from "../workflowEditorUtils";
import { WorkflowScopeContext } from "../WorkflowScopeContext";
import type { DocGraphEdits } from "./docEditApi";
import { block, code, conditional, forLoop, settings } from "./docTestBlocks";
import { WorkflowDocEditor } from "./WorkflowDocEditor";

vi.mock("./useDocLatestRunCompleted", () => ({
  useDocLatestRunCompleted: () => false,
}));
vi.mock("../workflowYamlDocument", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../workflowYamlDocument")>()),
  buildWorkflowSaveRequest: () => ({}),
}));
// The run button needs studio run contexts; this file tests the header's layout.
vi.mock("./DocRunStepButton", () => ({ DocRunStepButton: () => null }));
vi.mock("../panels/BlockConfigForm", () => ({ BlockConfigForm: () => null }));

beforeEach(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  vi.stubGlobal("CSS", { escape: (value: string) => value });
  Element.prototype.scrollIntoView = vi.fn();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const edits = (): DocGraphEdits => ({
  apply: vi.fn(),
  update: vi.fn(),
  addStep: vi.fn(() => null),
  duplicate: vi.fn(),
});

function renderDoc(blocks: WorkflowBlock[], readOnly = false) {
  const { nodes, edges } = getElements(blocks, settings(), true);
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <ReactFlowProvider>
          <WorkflowScopeContext.Provider value={{ workflowId: null, readOnly }}>
            <WorkflowDocEditor
              nodes={nodes}
              edges={edges}
              edits={readOnly ? undefined : edits()}
            />
          </WorkflowScopeContext.Provider>
        </ReactFlowProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return nodes;
}

const login = (label: string) =>
  block("login", label, null, {
    url: "https://example.com",
    navigation_goal: "Sign in",
    parameter_keys: [],
  });

// Row 1 is the element that holds the step's number; the title must be outside it and after it.
function header(label: string, nodes: ReturnType<typeof renderDoc>) {
  const id = nodes.find((n) => n.data.label === label)!.id;
  const step = document.querySelector(`[data-doc-node-id="${id}"]`)!;
  const number = step.querySelector('[data-testid="doc-step-number"]')!;
  const tag = step.querySelector("[data-doc-tag], span[class*='h-[21px]']")!;
  let row = number.parentElement!;
  while (!row.contains(tag)) row = row.parentElement!;
  return { step, row, tag };
}

function expectTitleBelow(row: Element, title: string) {
  const heading = screen.getByText(title);
  expect(row.contains(heading)).toBe(false);
  expect(
    row.compareDocumentPosition(heading) & Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy();
}

describe("Doc step header", () => {
  test("the number and type are on the first row and the title is the row below", () => {
    const nodes = renderDoc([login("sample_login")]);
    const { row, tag } = header("sample_login", nodes);
    expect(row.textContent).toMatch(/^1Login/);
    expectTitleBelow(row, "Sample login");
    // The type label carries no icon.
    expect(tag.querySelector("svg")).toBeNull();
  });

  test("a default name has no title row", () => {
    const nodes = renderDoc([login("login_1")]);
    const { step, row } = header("login_1", nodes);
    expect(row.nextElementSibling).toBeNull();
    expect(step.textContent).not.toMatch(/login_1|Login 1/);
  });

  test("the read-only Doc shows the same rows, with a plain type label", () => {
    const nodes = renderDoc([login("sample_login")], true);
    const { row, tag } = header("sample_login", nodes);
    expect(tag.tagName).toBe("SPAN");
    expect(tag.querySelector("svg")).toBeNull();
    expectTitleBelow(row, "Sample login");
  });

  test("the type label is still the button that opens the step's actions", () => {
    renderDoc([login("sample_login")]);
    const tag = screen.getByRole("button", {
      name: "Login step sample_login, actions",
    });
    expect(tag.hasAttribute("data-doc-tag")).toBe(true);
    fireEvent.click(tag);
    expect(screen.getByRole("menuitem", { name: /Settings/ })).toBeTruthy();
  });

  test("a loop keeps its arrow on the first row, above its title", () => {
    const nodes = renderDoc([
      forLoop("each_row", null, [code("inner_step", null)]),
    ]);
    const { row } = header("each_row", nodes);
    expect(
      row.contains(screen.getByRole("button", { name: /^Collapse each_row/ })),
    ).toBe(true);
    expectTitleBelow(row, "Each row");
  });

  test("an If step has the same order: number and type, then its title", () => {
    const nodes = renderDoc([
      conditional("check_status", null, [
        { id: "yes", next: "on_yes", when: "The status is ready" },
        { id: "no", next: "on_no" },
      ]),
      code("on_yes", null),
      code("on_no", null),
    ]);
    const { row } = header("check_status", nodes);
    expect(row.textContent).toMatch(/^1Conditional/);
    expect(
      row.contains(screen.getByRole("button", { name: /^(Collapse|Expand) / })),
    ).toBe(true);
    expectTitleBelow(row, "Check status");
  });
});
