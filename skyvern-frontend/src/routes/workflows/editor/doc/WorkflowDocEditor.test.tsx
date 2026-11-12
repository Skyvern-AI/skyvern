// @vitest-environment jsdom
import { clearDocAnnouncement, docAnnouncement } from "./docAnnounce";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import {
  afterEach,
  beforeEach,
  describe,
  expect,
  onTestFinished,
  test,
  vi,
} from "vitest";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ReactFlowProvider } from "@xyflow/react";
import { MemoryRouter } from "react-router-dom";

import { ProxyLocation } from "@/api/types";
import { useCopilotProposalStore } from "@/store/CopilotProposalStore";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";

import {
  WorkflowParameterValueType,
  type WorkflowApiResponse,
  type WorkflowBlock,
  type WorkflowSettings,
} from "../../types/workflowTypes";
import { isWorkflowBlockNode } from "../nodes";
import { getElements } from "../workflowEditorUtils";
import { useLocateBlockStore } from "../runValidation/useLocateBlockStore";
import {
  DOC_SHOW_PROBLEMS_EVENT,
  type DocShowProblemsDetail,
  captureReturnPoint,
} from "./docKeys";
import { docDetailsText } from "./docDetailsText";
import { useDocRunCheckStore } from "./docRunCheckStore";
import { WorkflowScopeContext } from "../WorkflowScopeContext";
import { StudioShellContext } from "../../studio/StudioShellContext";
import { DOC_COPILOT_NUDGE } from "./DocStarter";
import { WorkflowDocEditor } from "./WorkflowDocEditor";
import { useDocUnfinishedStore } from "./docUnfinished";
import { splitAtCaret } from "./docCaret";
import { block, conditional, forLoop, settings } from "./docTestBlocks";
import type { DocGraphEdits } from "./docEditApi";
import { useWorkflowYamlEditorStore } from "@/store/WorkflowYamlEditorStore";
import {
  noteDemonstrationStartFailed,
  useRecordingLauncherStore,
} from "@/store/useRecordingLauncherStore";

vi.mock("./useDocLatestRunCompleted", () => ({
  useDocLatestRunCompleted: () => false,
}));

import type { DocGraph } from "./docGraphEdits";
import * as axiosClientModule from "@/api/AxiosClient";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";

vi.mock("../workflowYamlDocument", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../workflowYamlDocument")>()),
  buildWorkflowSaveRequest: () => ({}),
}));

// The run button needs studio run contexts; this file tests navigation, so it is stubbed.
vi.mock("./DocRunStepButton", () => ({ DocRunStepButton: () => null }));
// The settings form is mocked because these tests check the side panel's header only.
vi.mock("../panels/BlockConfigForm", () => ({ BlockConfigForm: () => null }));
const openPane = vi.hoisted(() => vi.fn());
vi.mock("../../studio/useStudioPanes", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../studio/useStudioPanes")>();
  return {
    ...actual,
    useStudioPanes: () => ({ ...actual.useStudioPanes(), openPane }),
  };
});

const output = (label: string) => ({
  parameter_type: "output",
  key: `${label}_output`,
  output_parameter_id: `op-${label}`,
});
const code = (label: string, next: string | null) =>
  ({
    label,
    block_type: "code",
    next_block_label: next,
    continue_on_failure: false,
    model: null,
    output_parameter: output(label),
    code: "",
    parameters: [],
  }) as unknown as WorkflowBlock;
const loop = {
  label: "each_row",
  block_type: "for_loop",
  next_block_label: null,
  continue_on_failure: false,
  model: null,
  output_parameter: output("each_row"),
  loop_over: { key: "items" },
  loop_blocks: [code("inner_step", null)],
  loop_variable_reference: "{{ items }}",
  complete_if_empty: false,
} as unknown as WorkflowBlock;

function renderDoc(
  blocks: WorkflowBlock[] = [loop],
  edits?: DocGraphEdits,
  url = "/",
  workflowId: string | null = null,
) {
  const { nodes, edges } = getElements(
    blocks,
    {
      proxyLocation: ProxyLocation.Residential,
      finallyBlockLabel: null,
    } as unknown as WorkflowSettings,
    true,
  );
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter initialEntries={[url]}>
        <ReactFlowProvider>
          <WorkflowScopeContext.Provider
            value={{ workflowId, readOnly: false }}
          >
            <WorkflowDocEditor nodes={nodes} edges={edges} edits={edits} />
          </WorkflowScopeContext.Provider>
        </ReactFlowProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return nodes;
}

// A Doc whose edits change what it renders: rerender() shows the graph the last edit left.
function renderLiveDoc(blocks: WorkflowBlock[]) {
  let current = getElements(blocks, settings(), true) as DocGraph;
  const client = new QueryClient();
  const edits: DocGraphEdits = {
    apply: (next) => {
      current = next;
    },
    update: (change) => {
      current = { ...current, nodes: change(current.nodes) };
    },
    addStep: () => null,
    duplicate: () => {},
  };
  const view = () => (
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ReactFlowProvider>
          <WorkflowDocEditor {...current} edits={edits} />
        </ReactFlowProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
  const rendered = render(view());
  return {
    graph: () => current,
    rerender: () => rendered.rerender(view()),
  };
}

// The count of problems (not warnings) on the Doc's problems button.
const blockingCount = () =>
  Number(
    document
      .querySelector("[data-doc-problems]")
      ?.textContent?.match(/(\d+) problem/)?.[1] ?? 0,
  );

describe("WorkflowDocEditor navigation", () => {
  beforeEach(() => {
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
    useWorkflowParametersStore.setState({ parameters: [] });
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    );
    vi.stubGlobal("requestAnimationFrame", (cb: () => void) => {
      cb();
      return 0;
    });
    vi.stubGlobal("CSS", { escape: (value: string) => value });
    Element.prototype.scrollIntoView = vi.fn();
  });
  afterEach(() => {
    cleanup();
    useLocateBlockStore.getState().clearLocate();
    vi.unstubAllGlobals();
  });

  describe("the Copilot suggestion", () => {
    const edits = {
      apply: vi.fn(),
      update: vi.fn(),
      addStep: vi.fn(),
      duplicate: vi.fn(),
    };
    const nudge = () => document.querySelector("[data-doc-copilot-nudge]");
    const checklist = () =>
      screen.queryByRole("region", { name: "Getting started" }) ??
      screen.queryByLabelText(/^Getting started/);
    // The suggestion is shown once per browser; each case starts from a browser that has not seen it.
    beforeEach(() => {
      localStorage.removeItem("skyvern.doc.gettingStarted");
      openPane.mockClear();
    });
    afterEach(() => {
      localStorage.removeItem("skyvern.doc.gettingStarted");
      useCopilotProposalStore.getState().setProposal(null);
    });

    test("a fresh empty Doc suggests Copilot alone; Dismiss hides it for good and the getting-started aids come back", () => {
      renderDoc([], edits, "/", "wpid_1");
      // One onboarding aid at a time: no tip and no checklist beside the suggestion.
      const tipOrChecklist = () =>
        document.querySelector('[role="note"]:not([data-doc-copilot-nudge])') ??
        checklist();
      expect(nudge()).not.toBeNull();
      expect(tipOrChecklist()).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
      expect(nudge()).toBeNull();
      expect(tipOrChecklist()).not.toBeNull();
      cleanup();
      renderDoc([], edits, "/", "wpid_1");
      expect(nudge()).toBeNull();
    });

    test("Try Copilot opens the Copilot pane and the suggestion does not come back", () => {
      renderDoc([], edits);
      fireEvent.click(screen.getByRole("button", { name: "Try Copilot" }));
      expect(openPane).toHaveBeenCalledWith("copilot");
      expect(nudge()).toBeNull();
      cleanup();
      renderDoc([], edits);
      expect(nudge()).toBeNull();
    });

    test("writing in the blank step answers the suggestion: clearing the text again does not bring it back", () => {
      const client = new QueryClient();
      const view = (prompt: string) => {
        const { nodes, edges } = getElements(
          [block("text_prompt", "write_it", null, { prompt })],
          settings(),
          true,
        );
        return (
          <QueryClientProvider client={client}>
            <MemoryRouter>
              <ReactFlowProvider>
                <WorkflowDocEditor nodes={nodes} edges={edges} edits={edits} />
              </ReactFlowProvider>
            </MemoryRouter>
          </QueryClientProvider>
        );
      };
      const rendered = render(view(""));
      expect(nudge()).not.toBeNull();
      rendered.rerender(view("S"));
      expect(nudge()).toBeNull();
      rendered.rerender(view(""));
      expect(nudge()).toBeNull();
    });

    test("asking Copilot while the suggestion shows answers it: it stays answered after the proposal clears", () => {
      renderDoc([], edits, "/", "wpid_1");
      expect(nudge()).not.toBeNull();
      const proposed = {
        workflow_id: "w_1",
        workflow_permanent_id: "wpid_1",
        title: "Synthetic",
        description: null,
        workflow_definition: {
          version: 2,
          blocks: [block("text_prompt", "write_it", null, { prompt: "Hi" })],
          parameters: [],
        },
      } as unknown as WorkflowApiResponse;
      act(() =>
        useCopilotProposalStore.getState().setProposal({
          proposed,
          baseline: {
            ...proposed,
            workflow_definition: { version: 2, blocks: [], parameters: [] },
          },
          review: null,
          actionable: true,
          acceptable: true,
          decide: async () => true,
        }),
      );
      expect(nudge()).toBeNull();
      act(() => useCopilotProposalStore.getState().setProposal(null));
      expect(nudge()).toBeNull();
      cleanup();
      renderDoc([], edits, "/", "wpid_1");
      expect(nudge()).toBeNull();
    });

    test.each([
      ["closed, folded, or open but not on screen", false, true],
      ["on screen beside the Doc", true, false],
    ])(
      "with Copilot %s, the suggestion offers Try Copilot only when Copilot is not on screen",
      (_state, onScreen, offersTry) => {
        render(
          <QueryClientProvider client={new QueryClient()}>
            <MemoryRouter>
              <ReactFlowProvider>
                <StudioShellContext.Provider
                  value={{
                    copilotPortalEl: null,
                    panelPortalEl: null,
                    setEditorStreamSlot: () => {},
                    setBrowserStreamSlot: () => {},
                    setRunStreamSlot: () => {},
                    isPaneOnScreen: (id) => onScreen && id === "copilot",
                  }}
                >
                  <WorkflowDocEditor nodes={[]} edges={[]} edits={edits} />
                </StudioShellContext.Provider>
              </ReactFlowProvider>
            </MemoryRouter>
          </QueryClientProvider>,
        );
        expect(nudge()?.textContent).toContain(
          offersTry ? DOC_COPILOT_NUDGE.text : DOC_COPILOT_NUDGE.textBeside,
        );
        expect(
          screen.queryByRole("button", { name: "Try Copilot" }) !== null,
        ).toBe(offersTry);
      },
    );

    test("one step with blank text suggests it; a step with text or a step with no text to write does not", () => {
      renderDoc(
        [block("text_prompt", "write_it", null, { prompt: "" })],
        edits,
      );
      expect(nudge()).not.toBeNull();
      cleanup();
      renderDoc(
        [
          block("text_prompt", "write_it", null, {
            prompt: "Summarize the page",
          }),
        ],
        edits,
      );
      expect(nudge()).toBeNull();
      cleanup();
      renderDoc([block("wait", "pause", null, { wait_sec: 5 })], edits);
      expect(nudge()).toBeNull();
      cleanup();
      // A URL is a setting beyond text: the step is configured, not blank.
      renderDoc(
        [
          block("navigation", "open_page", null, {
            navigation_goal: "",
            url: "https://example.com",
          }),
        ],
        edits,
      );
      expect(nudge()).toBeNull();
    });
  });

  describe("keyboard-first editing", () => {
    const editsWithAdd = (): DocGraphEdits => ({
      apply: vi.fn(),
      update: vi.fn(),
      addStep: vi.fn(() => "new_step"),
      duplicate: vi.fn(),
    });
    // Opens the line and puts the caret `at` characters in; jsdom has no innerText, so the text is set both ways.
    const openLine = (name: string, text: string, at: number) => {
      fireEvent.focus(screen.getByRole("textbox", { name }));
      const box = screen.getByRole("textbox", { name });
      Object.defineProperty(box, "innerText", {
        value: text,
        configurable: true,
      });
      box.textContent = text;
      const range = document.createRange();
      if (box.firstChild) range.setStart(box.firstChild, at);
      else range.setStart(box, 0);
      range.collapse(true);
      window.getSelection()?.removeAllRanges();
      window.getSelection()?.addRange(range);
      return box;
    };
    const prompt = () =>
      block("text_prompt", "write_it", null, { prompt: "Do the next thing" });

    test("the insert line between steps is not rendered on a touch screen, whatever the width", () => {
      const steps = () => [
        block("goto_url", "open_site", "write_it", { url: "https://a.test" }),
        prompt(),
      ];
      const stubPointer = (touch: boolean) =>
        vi.stubGlobal("matchMedia", (query: string) => ({
          matches: query === "(hover: none), (pointer: coarse)" ? touch : false,
          addEventListener: () => {},
          removeEventListener: () => {},
        }));

      stubPointer(false);
      renderDoc(steps(), editsWithAdd());
      expect(
        screen.getByRole("button", { name: "Add a step here" }),
      ).toBeTruthy();
      cleanup();

      stubPointer(true);
      renderDoc(steps(), editsWithAdd());
      expect(
        screen.queryByRole("button", { name: "Add a step here" }),
      ).toBeNull();
      expect(document.querySelector("[data-doc-insert-line]")).toBeNull();
      expect(screen.getByRole("button", { name: /Add step/ })).toBeTruthy();
    });

    test("Escape out of a step's text leaves focus on that step's type label", () => {
      renderDoc([prompt()], editsWithAdd());
      const box = openLine("write_it prompt", "Do the next thing", 17);
      box.focus();
      fireEvent.keyDown(box, { key: "Escape" });
      expect(document.activeElement).toBe(
        document.querySelector("[data-doc-tag]"),
      );
    });

    test("Search shows its ⌘K key only where there is a keyboard", () => {
      const stubPointer = (touch: boolean) =>
        vi.stubGlobal("matchMedia", (query: string) => ({
          matches: query === "(hover: none), (pointer: coarse)" ? touch : false,
          addEventListener: () => {},
          removeEventListener: () => {},
        }));
      stubPointer(false);
      renderDoc([prompt()], editsWithAdd());
      expect(
        screen.getByRole("button", { name: /^Search/ }).textContent,
      ).toContain("⌘K");
      cleanup();
      stubPointer(true);
      renderDoc([prompt()], editsWithAdd());
      expect(
        screen.getByRole("button", { name: /^Search/ }).textContent,
      ).not.toContain("⌘K");
    });

    test("adding a step with / announces it", () => {
      clearDocAnnouncement();
      renderDoc(
        [block("goto_url", "empty_one", null, { url: "" }), prompt()],
        editsWithAdd(),
      );
      fireEvent.keyDown(openLine("empty_one url", "", 0), { key: "/" });
      fireEvent.click(screen.getByText(/^Loop/));
      expect(docAnnouncement()).toBe("Added Loop step");
    });

    test("Enter at the end of a step adds a step of the same type below", () => {
      const edits = editsWithAdd();
      const nodes = renderDoc([prompt()], edits);
      const box = openLine("write_it prompt", "Do the next thing", 17);
      fireEvent.keyDown(box, { key: "Enter" });

      expect(edits.addStep).toHaveBeenCalledTimes(1);
      expect(vi.mocked(edits.addStep).mock.calls[0]![0].nodeType).toBe(
        nodes.find(isWorkflowBlockNode)!.type,
      );
    });

    test("Enter in the middle of a step splits its text between the two steps", () => {
      const edits = editsWithAdd();
      const nodes = renderDoc([prompt()], edits);
      const box = openLine("write_it prompt", "Do the next thing", 6);
      fireEvent.keyDown(box, { key: "Enter" });

      expect(edits.addStep).toHaveBeenCalledTimes(1);
      const updates = vi.mocked(edits.update).mock.calls;
      const last = updates[updates.length - 1]![0];
      const first = last(nodes).find(isWorkflowBlockNode)!.data as Record<
        string,
        unknown
      >;
      expect(first.prompt).toBe("Do the");
    });

    test("Enter on an empty step turns it into a note instead of adding another step", () => {
      const edits = editsWithAdd();
      renderDoc(
        [
          block("goto_url", "open_site", "empty_one", {
            url: "https://a.test",
          }),
          block("goto_url", "empty_one", null, { url: "" }),
        ],
        edits,
      );
      const box = openLine("empty_one url", "", 0);
      fireEvent.keyDown(box, { key: "Enter" });

      expect(edits.addStep).not.toHaveBeenCalled();
      const applied = vi.mocked(edits.apply).mock.calls[0]![0].nodes;
      expect(
        applied.filter(isWorkflowBlockNode).map((n) => n.data.label),
      ).toEqual(["open_site"]);
    });

    test.each([
      ["print_page", "printed", { include_timestamp: true }],
      ["wait", "pause", { wait_sec: 1 }],
    ])(
      "Enter on a %s step that needs no text keeps it and opens an empty line below",
      (type, label, extra) => {
        const edits = editsWithAdd();
        renderDoc(
          [
            block("goto_url", "open_site", label, { url: "https://a.test" }),
            block(type, label, null, extra),
          ],
          edits,
        );
        const box = openLine(`${label} description`, "", 0);
        fireEvent.keyDown(box, { key: "Enter" });

        expect(edits.addStep).not.toHaveBeenCalled();
        expect(edits.apply).not.toHaveBeenCalled();
        expect(screen.getByRole("textbox", { name: "Note" })).toBeTruthy();
      },
    );

    test("Enter on an empty step that needs text still turns it into a note (text prompt)", () => {
      const edits = editsWithAdd();
      renderDoc(
        [
          block("goto_url", "open_site", "empty_one", {
            url: "https://a.test",
          }),
          block("text_prompt", "empty_one", null, { prompt: "" }),
        ],
        edits,
      );
      const box = openLine("empty_one prompt", "", 0);
      fireEvent.keyDown(box, { key: "Enter" });

      expect(edits.addStep).not.toHaveBeenCalled();
      expect(edits.apply).toHaveBeenCalled();
    });

    test("Picking a loop with / on an empty step replaces the step", () => {
      const edits = editsWithAdd();
      renderDoc(
        [block("goto_url", "empty_one", null, { url: "" }), prompt()],
        edits,
      );
      const box = openLine("empty_one url", "", 0);
      fireEvent.keyDown(box, { key: "/" });
      fireEvent.click(screen.getByText(/^Loop/));

      expect(vi.mocked(edits.addStep).mock.calls[0]![0].nodeType).toBe("loop");
    });

    test("Shift+Enter is a line break inside the step, not a new step", () => {
      const edits = editsWithAdd();
      renderDoc([prompt()], edits);
      const box = openLine("write_it prompt", "Do the next thing", 17);
      const proceeded = fireEvent.keyDown(box, {
        key: "Enter",
        shiftKey: true,
      });

      expect(proceeded).toBe(true);
      expect(edits.addStep).not.toHaveBeenCalled();
    });

    test("a save while the caret is in a step leaves its text on screen", () => {
      // jsdom has no innerText; the line reads and writes its text through it.
      const innerText = Object.getOwnPropertyDescriptor(
        HTMLElement.prototype,
        "innerText",
      );
      Object.defineProperty(HTMLElement.prototype, "innerText", {
        configurable: true,
        get(this: HTMLElement) {
          return this.textContent ?? "";
        },
        set(this: HTMLElement, text: string) {
          this.textContent = text;
        },
      });
      try {
        const goal = "Find the book and return its price";
        renderDoc(
          [
            block("extraction", "find_it", null, {
              data_extraction_goal: goal,
            }),
          ],
          editsWithAdd(),
        );
        fireEvent.focus(
          screen.getByRole("textbox", { name: "find_it dataExtractionGoal" }),
        );
        const typing = screen.getByRole("textbox", {
          name: "find_it dataExtractionGoal",
        });
        expect(typing.textContent).toBe(goal);
        const caret = document.createRange();
        caret.setStart(typing.firstChild!, "Find the ".length);
        caret.setEnd(typing.firstChild!, "Find the book".length);
        window.getSelection()?.removeAllRanges();
        window.getSelection()?.addRange(caret);
        fireEvent.keyDown(typing, { key: "s", metaKey: true });

        // Saving holds the editor lock until the request settles.
        act(() =>
          useWorkflowYamlEditorStore.setState({ commitInProgress: true }),
        );
        act(() =>
          useWorkflowYamlEditorStore.setState({ commitInProgress: false }),
        );

        const line = screen.getByRole("textbox", {
          name: "find_it dataExtractionGoal",
        });
        expect(line.textContent).toBe(goal);
        expect(document.activeElement).toBe(line);
        expect(splitAtCaret(line)).toEqual([
          "Find the ",
          " and return its price",
        ]);
      } finally {
        if (innerText)
          Object.defineProperty(HTMLElement.prototype, "innerText", innerText);
        else delete (HTMLElement.prototype as { innerText?: string }).innerText;
      }
    });

    test("Tab and Shift+Tab say out loud where the step moved, for a loop and for a branch", () => {
      const status = () => screen.getByRole("status").textContent?.trim();
      const tab = (name: string, shiftKey: boolean) => {
        fireEvent.focus(screen.getByRole("textbox", { name }));
        return fireEvent.keyDown(screen.getByRole("textbox", { name }), {
          key: "Tab",
          shiftKey,
        });
      };

      renderDoc(
        [
          forLoop("each_book", "after", [code("inside", null)]),
          block("text_prompt", "after", null, { prompt: "Sum it" }),
        ],
        editsWithAdd(),
      );
      expect(status()).toBe("");
      expect(tab("after prompt", false)).toBe(false);
      expect(status()).toBe("Moved into the loop");
      cleanup();

      renderDoc(
        [
          forLoop("each_book", null, [
            block("text_prompt", "inner", null, { prompt: "Read it" }),
          ]),
        ],
        editsWithAdd(),
      );
      expect(tab("inner prompt", true)).toBe(false);
      expect(status()).toBe("Moved out of the loop");
      cleanup();

      renderDoc(
        [
          conditional("route", "after", [
            { id: "yes", next: "if_step", when: "Check the condition" },
            { id: "no", next: null },
          ]),
          code("if_step", "after"),
          block("text_prompt", "after", null, { prompt: "Sum it" }),
        ],
        editsWithAdd(),
      );
      expect(tab("after prompt", false)).toBe(false);
      expect(status()).toBe("Moved into the branch");
    });

    test("Tab on a line with nothing to nest is not prevented, so focus can move on", () => {
      renderDoc([block("extraction", "find_it", null, {})], editsWithAdd());
      const name = "find_it dataExtractionGoal";
      fireEvent.focus(screen.getByRole("textbox", { name }));
      const line = screen.getByRole("textbox", { name });
      expect(fireEvent.keyDown(line, { key: "Tab" })).toBe(true);
      expect(fireEvent.keyDown(line, { key: "Tab", shiftKey: true })).toBe(
        true,
      );
    });

    test("Tab on a note under an empty loop nests the line, opens no menu, and / then picks its step", async () => {
      const edits = editsWithAdd();
      renderDoc(
        [
          forLoop("books", null, [], {
            trailing_notes: "Open each book page",
          }),
        ],
        edits,
      );
      const box = openLine("Note", "Open each book page", 19);
      fireEvent.keyDown(box, { key: "Tab" });
      await act(() => new Promise((r) => requestAnimationFrame(r)));

      expect(screen.queryByPlaceholderText("Add a step…")).toBeNull();
      expect(edits.addStep).not.toHaveBeenCalled();
      const nested = screen.getByRole("textbox", { name: "Note" });
      expect(nested.closest(".border-l")).toBeTruthy();

      fireEvent.keyDown(nested, { key: "/" });
      expect(screen.getByPlaceholderText("Add a step…")).toBeTruthy();
    });

    test("Tab into a collapsed empty loop opens it, says nothing moved yet, and puts the caret at the line's start", async () => {
      renderDoc(
        [
          forLoop("books", null, [], {
            trailing_notes: "Open each book page",
          }),
        ],
        editsWithAdd(),
      );
      fireEvent.click(screen.getByRole("button", { name: /^Collapse/ }));
      const box = openLine("Note", "Open each book page", 19);
      fireEvent.keyDown(box, { key: "Tab" });
      await act(() => new Promise((r) => requestAnimationFrame(r)));

      expect(docAnnouncement()).toBe("In the loop. Type / to pick its step");
      const nested = screen.getByRole("textbox", { name: "Note" });
      expect(nested.closest(".border-l")).toBeTruthy();
    });
  });

  test.each([
    [
      "code",
      { code: "", parameters: [] },
      "Run Python code",
      "prompt",
      "Get the top five posts",
    ],
    ["goto_url", { url: "" }, "Go to a URL", "url", "https://example.com/"],
    [
      "email_inbox",
      { prompt: "" },
      "Find an email",
      "prompt",
      "Latest invoice",
    ],
  ])(
    "typing in a %s step's line edits the field that drives it, not its description",
    (blockType, extra, placeholder, key, typed) => {
      const edits: DocGraphEdits = {
        apply: vi.fn(),
        update: vi.fn(),
        addStep: vi.fn(),
        duplicate: vi.fn(),
      };
      const nodes = renderDoc(
        [block(blockType, "the_step", null, extra)],
        edits,
      );
      expect(screen.getByText(placeholder)).toBeTruthy();
      const name = `the_step ${key}`;
      fireEvent.focus(screen.getByRole("textbox", { name }));
      const box = screen.getByRole("textbox", { name });
      // jsdom has no innerText; the line reads it on input.
      Object.defineProperty(box, "innerText", {
        value: typed,
        configurable: true,
      });
      fireEvent.input(box);

      const updater = vi.mocked(edits.update).mock.calls[0]![0];
      const data = updater(nodes).find(isWorkflowBlockNode)!.data as Record<
        string,
        unknown
      >;
      expect(data[key]).toBe(typed);
      expect(data.description).not.toBe(typed);
    },
  );

  test.each([
    ["navigation", { navigation_goal: "" }, /agent should do\. Type @/],
    ["validation", { complete_criterion: "" }, /check to pass\. Type @/],
    [
      "human_interaction",
      { instructions: "", recipients: [] },
      /before deciding\. Type @/,
    ],
  ])(
    "an empty %s step shows its editing hint while editable and No instructions yet when read-only",
    (blockType, extra, hint) => {
      const step = () => [block(blockType, "the_step", null, extra)];
      renderDoc(step(), {
        apply: vi.fn(),
        update: vi.fn(),
        addStep: vi.fn(),
        duplicate: vi.fn(),
      });
      expect(screen.getByText(hint)).toBeTruthy();
      cleanup();
      renderDoc(step());
      expect(screen.queryByText(hint)).toBeNull();
      expect(screen.getAllByText("No instructions yet").length).toBeGreaterThan(
        0,
      );
    },
  );

  test("a read-only doc reserves no empty row above Inputs", () => {
    renderDoc([block("goto_url", "open_site", null, { url: "x" })]);
    expect(document.querySelector('[aria-hidden="true"].h-\\[28px\\]')).toBe(
      null,
    );
  });

  test("a Go to URL step with no URL shows a required chip", () => {
    renderDoc([block("goto_url", "open_site", null, { url: "" })]);
    expect(document.querySelector('[data-doc-missing="URL"]')).toBeTruthy();
  });

  test("a step offers its prompt's URL for its empty URL field after Details, with the reason, and not while typing or read-only", () => {
    const task = block("task", "search_step", "next_step", {
      url: "",
      navigation_goal: "Go to https://example.com/page and search for shoes",
    });
    const next = block("task", "next_step", null, {
      url: "",
      navigation_goal: "Then open https://example.com/other",
    });
    const edits: DocGraphEdits = {
      apply: vi.fn(),
      update: vi.fn(),
      addStep: vi.fn(),
      duplicate: vi.fn(),
    };
    const nodes = renderDoc([task, next], edits);
    const offer = () =>
      screen.queryByRole("button", {
        name: "Use https://example.com/page as the URL",
      });
    expect(
      screen.getByText("This step needs a page to start on."),
    ).toBeTruthy();
    expect(
      screen.getByText(
        "Without a URL, this step starts on the previous step's page.",
      ),
    ).toBeTruthy();

    const mainLine = screen.getByRole("textbox", {
      name: "search_step navigationGoal",
    });
    fireEvent.focus(mainLine);
    expect(offer()).toBe(null);
    fireEvent.blur(
      screen.getByRole("textbox", { name: "search_step navigationGoal" }),
    );

    // Forward Tab follows the DOM: the prompt, then Details, then the offer.
    const tabbable = [
      ...document.querySelectorAll<HTMLElement>('[role="textbox"], button'),
    ];
    const at = (el: HTMLElement) => tabbable.indexOf(el);
    const prompt = screen.getByRole("textbox", {
      name: "search_step navigationGoal",
    });
    const options = screen.getByRole("button", {
      name: /^Details for .*search/i,
    });
    expect(at(prompt)).toBeLessThan(at(options));
    expect(at(options)).toBeLessThan(at(offer()!));
    expect(
      tabbable
        .slice(at(options) + 1, at(offer()!))
        .some((el) => el.getAttribute("role") === "textbox"),
    ).toBe(false);

    fireEvent.click(offer()!);
    const updater = vi.mocked(edits.update).mock.calls[0]![0];
    const data = updater(nodes).find(isWorkflowBlockNode)!.data as Record<
      string,
      unknown
    >;
    expect(data.url).toBe("https://example.com/page");
    expect(data.navigationGoal).toBe(
      "Go to https://example.com/page and search for shoes",
    );
    expect(docAnnouncement()).toBe("URL set to https://example.com/page");
    expect(document.activeElement?.getAttribute("aria-label")).toBe(
      "search_step navigationGoal",
    );

    // Dismissing an offer puts focus back on its step's prompt line.
    const nextOffer = document.querySelector<HTMLElement>(
      '[data-doc-suggestion="url"]',
    )!;
    fireEvent.click(within(nextOffer).getByRole("button", { name: "Dismiss" }));
    expect(nextOffer.isConnected).toBe(false);
    expect(document.activeElement?.getAttribute("aria-label")).toBe(
      "next_step navigationGoal",
    );

    cleanup();
    renderDoc([task, next]);
    expect(offer()).toBe(null);
  });

  test("a step's URL offer comes back after a demonstration takes editing away while its prompt is focused and focus moves on", () => {
    renderDoc(
      [
        block("task", "focus_step", null, {
          url: "",
          navigation_goal: "Go to https://example.com/focus and search",
        }),
      ],
      { apply: vi.fn(), update: vi.fn(), addStep: vi.fn(), duplicate: vi.fn() },
    );
    const offer = () =>
      screen.queryByRole("button", {
        name: "Use https://example.com/focus as the URL",
      });
    fireEvent.focus(
      screen.getByRole("textbox", { name: "focus_step navigationGoal" }),
    );
    expect(offer()).toBe(null);
    act(() =>
      useRecordingLauncherStore.setState({
        demonstration: {
          phase: "recording",
          actionCount: 0,
          interrupted: false,
          insertion: null,
        },
      }),
    );
    // Focus moves on during the demonstration (to the control that started it), so the line does not take it back.
    const elsewhere = document.body.appendChild(
      document.createElement("button"),
    );
    act(() => elsewhere.focus());
    act(() => useRecordingLauncherStore.setState({ demonstration: null }));
    expect(document.activeElement).toBe(elsewhere);
    expect(offer()).not.toBe(null);
    elsewhere.remove();
  });

  test("Locate on a step inside a collapsed loop reopens the loop and consumes the request", () => {
    const nodes = renderDoc();
    const inner = nodes.find((n) => n.data.label === "inner_step")!;
    expect(
      document.querySelector(`[data-doc-node-id="${inner.id}"]`),
    ).not.toBeNull();

    const toggle = screen
      .getAllByRole("button")
      .find((b) => b.getAttribute("aria-expanded") === "true")!;
    fireEvent.click(toggle);
    expect(
      document.querySelector(`[data-doc-node-id="${inner.id}"]`),
    ).toBeNull();

    act(() => useLocateBlockStore.getState().requestLocate(inner.id));

    expect(
      document.querySelector(`[data-doc-node-id="${inner.id}"]`),
    ).not.toBeNull();
    expect(useLocateBlockStore.getState().request).toBeNull();
  });

  test("jumping to a step flashes it", async () => {
    const animate = vi.fn();
    HTMLElement.prototype.animate = animate;
    vi.stubGlobal("matchMedia", () => ({ matches: false }));
    vi.stubGlobal("requestAnimationFrame", (cb: () => void) => cb());
    const nodes = renderDoc();
    const inner = nodes.find((n) => n.data.label === "inner_step")!;
    act(() => useLocateBlockStore.getState().requestLocate(inner.id));
    expect(animate).toHaveBeenCalledTimes(1);
    // @ts-expect-error cleanup of the jsdom stub
    delete HTMLElement.prototype.animate;
  });

  test("opens step search from Ctrl+K anywhere in the document", () => {
    renderDoc([
      block("goto_url", "find_by_label", null, {
        description: "Open the internal portal",
        url: "https://example.test/unique-path",
      }),
    ]);
    fireEvent.keyDown(
      screen.getByRole("region", { name: "Workflow document" }),
      {
        key: "k",
        ctrlKey: true,
      },
    );

    const input = screen.getByPlaceholderText("Search steps…");
    fireEvent.change(input, { target: { value: "find_by_label" } });
    expect(screen.getAllByRole("option")).toHaveLength(1);
  });

  test("the branch actions button shows only when the branch has an action", () => {
    const edits: DocGraphEdits = {
      apply: vi.fn(),
      update: vi.fn(),
      addStep: vi.fn(),
      duplicate: vi.fn(),
    };
    renderDoc(
      [
        conditional("route", null, [
          { id: "yes", next: "if_step", when: "found" },
          { id: "otherwise", next: null },
        ]),
        code("if_step", null),
      ],
      edits,
    );
    expect(screen.queryByRole("button", { name: /Branch actions/ })).toBeNull();
  });

  test("collapse all closes loops and conditionals", () => {
    const nodes = renderDoc([
      forLoop("each_row", "route", [code("inner_loop", null)]),
      conditional("route", null, [
        { id: "yes", next: "inner_branch", when: "found" },
        { id: "otherwise", next: null },
      ]),
      code("inner_branch", null),
    ]);
    const loopChild = nodes.find((node) => node.data.label === "inner_loop")!;
    const branchChild = nodes.find(
      (node) => node.data.label === "inner_branch",
    )!;

    fireEvent.click(screen.getByRole("button", { name: "Collapse all" }));

    expect(
      document.querySelector(`[data-doc-node-id="${loopChild.id}"]`),
    ).toBeNull();
    expect(
      document.querySelector(`[data-doc-node-id="${branchChild.id}"]`),
    ).toBeNull();
  });

  test("counts server validation problems in the edit bar", async () => {
    vi.useFakeTimers();
    const post = vi.fn().mockResolvedValue({
      data: {
        problems: [
          {
            code: "unknown_reference",
            block_label: "inner_step",
            field: "prompt",
            message: "Unknown reference: missing_value.",
          },
          {
            code: "unknown_reference",
            block_label: "inner_step",
            field: "url",
            message: "Unknown reference: missing_value.",
          },
          {
            code: "schema_error",
            block_label: "inner_step",
            message: "This block has an invalid setting.",
          },
          {
            code: "unknown_loop_source",
            block_label: "inner_step",
            severity: "warning",
            message: "The loop source isn't a known input.",
          },
          {
            code: "template_too_complex",
            block_label: "inner_step",
            message: "This expression is nested too deeply to check.",
          },
          {
            code: "branch_only_reference",
            block_label: "inner_step",
            severity: "warning",
            message:
              "x is only produced on some branches of c, so it may be empty here.",
          },
          {
            code: "invalid_draft",
            block_label: null,
            message: "The draft could not be read.",
          },
          {
            code: "some_new_code",
            block_label: "inner_step",
            message: "A problem the Doc does not know yet.",
          },
        ],
      },
    });
    vi.spyOn(axiosClientModule, "getClient").mockResolvedValue({
      post,
    } as never);
    const getSaveData = useWorkflowHasChangesStore.getState().getSaveData;
    useWorkflowHasChangesStore.setState({ getSaveData: () => ({}) as never });

    try {
      renderDoc();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000);
      });

      // The same reference in two fields is one problem; warnings and checks the server could not finish never ask before Run.
      expect(screen.getByText("2 problems, 5 warnings")).toBeTruthy();
      expect(blockingCount()).toBe(2);
      expect(
        screen.getByText(/This block has an invalid setting\./),
      ).toBeTruthy();
      expect(
        useDocRunCheckStore.getState().critical.map((p) => p.message),
      ).toEqual([
        "Unknown reference: missing_value.",
        "This block has an invalid setting.",
      ]);
      cleanup();
      expect(useDocRunCheckStore.getState().critical).toEqual([]);
    } finally {
      useWorkflowHasChangesStore.setState({ getSaveData });
      vi.useRealTimers();
      vi.restoreAllMocks();
      cleanup();
    }
  });

  test("a credential named in a note is a problem even though the server sends it as a warning", async () => {
    vi.useFakeTimers();
    const post = vi.fn().mockResolvedValue({
      data: {
        problems: [
          {
            code: "credential_in_note",
            severity: "warning",
            block_label: "inner_step",
            field: "notes",
            message: "Notes can't use a credential input.",
          },
          {
            code: "credential_in_note",
            severity: "warning",
            block_label: "inner_step",
            field: "trailing_notes",
            message: "Notes can't use a credential input.",
          },
        ],
      },
    });
    vi.spyOn(axiosClientModule, "getClient").mockResolvedValue({
      post,
    } as never);
    const getSaveData = useWorkflowHasChangesStore.getState().getSaveData;
    useWorkflowHasChangesStore.setState({ getSaveData: () => ({}) as never });
    try {
      renderDoc();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000);
      });
      // Named in the note above and the trailing note: one entry, and Run counts that one.
      expect(screen.getByText("1 problem")).toBeTruthy();
      expect(blockingCount()).toBe(1);
    } finally {
      useWorkflowHasChangesStore.setState({ getSaveData });
      vi.useRealTimers();
      vi.restoreAllMocks();
      cleanup();
    }
  });

  test("shows a 413 validation failure without dropping the previous credential warning", async () => {
    vi.useFakeTimers();
    const post = vi
      .fn()
      .mockResolvedValueOnce({
        data: {
          problems: [
            {
              code: "credential_in_note",
              block_label: "inner_step",
              field: "notes",
              message: "Notes can't use a credential input.",
            },
          ],
        },
      })
      .mockRejectedValue({
        isAxiosError: true,
        response: {
          status: 413,
          data: {
            detail: "Workflow validation drafts must be at most 100000 bytes.",
          },
        },
      });
    vi.spyOn(axiosClientModule, "getClient").mockResolvedValue({
      post,
    } as never);
    const getSaveData = useWorkflowHasChangesStore.getState().getSaveData;
    useWorkflowHasChangesStore.setState({ getSaveData: () => ({}) as never });
    try {
      renderDoc();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000);
      });
      expect(screen.getByText("1 problem")).toBeTruthy();
      expect(blockingCount()).toBe(1);
      act(() => useWorkflowParametersStore.setState({ parameters: [] }));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000);
      });
      expect(screen.getByRole("alert").textContent).toContain(
        "Couldn't check this workflow: Workflow validation drafts must be at most 100000 bytes.",
      );
      expect(screen.getByText("2 warnings")).toBeTruthy();
      // The earlier check's warning describes an older draft, so it no longer holds the run.
      expect(blockingCount()).toBe(0);
    } finally {
      useWorkflowHasChangesStore.setState({ getSaveData });
      vi.useRealTimers();
      vi.restoreAllMocks();
      cleanup();
    }
  });

  test("shows workflow-level validation problems", async () => {
    vi.useFakeTimers();
    const post = vi.fn().mockResolvedValue({
      data: {
        valid: false,
        problems: [
          {
            block_label: null,
            code: "validation_limit",
            message: "Workflow validation supports at most 500 blocks.",
          },
        ],
      },
    });
    vi.spyOn(axiosClientModule, "getClient").mockResolvedValue({
      post,
    } as never);
    const getSaveData = useWorkflowHasChangesStore.getState().getSaveData;
    useWorkflowHasChangesStore.setState({ getSaveData: () => ({}) as never });

    try {
      renderDoc();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000);
      });

      expect(screen.getByRole("alert").textContent).toContain(
        "Workflow validation supports at most 500 blocks.",
      );
      // The server could not finish the check, so Run does not ask about it.
      expect(screen.getByText("1 warning")).toBeTruthy();
    } finally {
      useWorkflowHasChangesStore.setState({ getSaveData });
      vi.useRealTimers();
      vi.restoreAllMocks();
      cleanup();
    }
  });

  test("a warning-only validation response shows the problem as a warning, not a problem", async () => {
    vi.useFakeTimers();
    const post = vi.fn().mockResolvedValue({
      data: {
        valid: true,
        problems: [
          {
            block_label: null,
            code: "unknown_loop_source",
            severity: "warning",
            message: "The loop source isn't a known input.",
          },
        ],
      },
    });
    vi.spyOn(axiosClientModule, "getClient").mockResolvedValue({
      post,
    } as never);
    const getSaveData = useWorkflowHasChangesStore.getState().getSaveData;
    useWorkflowHasChangesStore.setState({ getSaveData: () => ({}) as never });
    try {
      renderDoc();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000);
      });
      expect(screen.getByText("1 warning")).toBeTruthy();
      expect(blockingCount()).toBe(0);
    } finally {
      useWorkflowHasChangesStore.setState({ getSaveData });
      vi.useRealTimers();
      vi.restoreAllMocks();
      cleanup();
    }
  });

  test("Tab reveals a moved step after the asynchronous graph update reaches a collapsed loop", () => {
    const graph = getElements(
      [
        forLoop("each", "after", [code("inner", null)]),
        block("text_prompt", "after", null, {
          prompt: "Write a summary",
          parameters: [],
        }),
      ],
      settings(),
      true,
    ) as DocGraph;
    const moved = graph.nodes.find((node) => node.data.label === "after")!;
    const parent = graph.nodes.find((node) => node.data.label === "each")!;
    let pending = graph;
    const client = new QueryClient();
    const edits = {
      apply: (next: DocGraph) => {
        pending = next;
      },
      update: () => {},
      addStep: () => null,
      duplicate: () => {},
    };
    const view = (current: DocGraph) => (
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <ReactFlowProvider>
            <WorkflowDocEditor {...current} edits={edits} />
          </ReactFlowProvider>
        </MemoryRouter>
      </QueryClientProvider>
    );
    const rendered = render(view(graph));
    const toggle = screen
      .getAllByRole("button")
      .find((button) => button.getAttribute("aria-expanded") === "true")!;
    fireEvent.click(toggle);
    fireEvent.click(screen.getByText("Write a summary"));
    const prompt = screen.getByRole("textbox", { name: "after prompt" });

    fireEvent.keyDown(prompt, { key: "Tab" });

    expect(pending.nodes.find((node) => node.id === moved.id)?.parentId).toBe(
      parent.id,
    );
    expect(useLocateBlockStore.getState().request?.nodeId).toBe(moved.id);
    rendered.rerender(view(pending));
    expect(
      document.querySelector(`[data-doc-node-id="${moved.id}"]`),
    ).not.toBeNull();
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(useLocateBlockStore.getState().request).toBeNull();
  });

  test("steps are numbered in their list, a loop's steps under the loop's number, and a named step shows its name in words", () => {
    const nodes = renderDoc([
      block("extraction", "extraction_1", "find_prices", {
        data_extraction_goal: "Read the page",
      }),
      block("extraction", "find_prices", "each_row", {
        data_extraction_goal: "Get the prices",
      }),
      loop,
    ]);
    const step = (label: string) =>
      within(
        document.querySelector<HTMLElement>(
          `[data-doc-node-id="${nodes.find((n) => n.data.label === label)!.id}"]`,
        )!,
      );
    expect(step("extraction_1").getByText("1")).toBeTruthy();
    expect(step("find_prices").getByText("2")).toBeTruthy();
    expect(step("each_row").getByText("3")).toBeTruthy();
    expect(step("inner_step").getByText("3.1")).toBeTruthy();
    expect(step("find_prices").getByText("Find prices")).toBeTruthy();
    expect(step("extraction_1").queryByText("Extraction 1")).toBeNull();
    // A for-each loop's unused while condition is not listed as something it uses.
    expect(step("each_row").queryByText("true")).toBeNull();
  });

  test("a conditional's branches continue one count, so no two branches share a step number, and the selected condition shows once", () => {
    const nodes = renderDoc([
      conditional("route", "after", [
        { id: "yes", next: "first_if", when: "Check the condition" },
        { id: "no", next: "only_else" },
      ]),
      code("first_if", "second_if"),
      code("second_if", "after"),
      code("only_else", "after"),
      code("after", null),
    ]);
    const number = (label: string) =>
      document.querySelector(
        `[data-doc-node-id="${nodes.find((n) => n.data.label === label)!.id}"] [data-testid="doc-step-number"]`,
      )?.textContent;
    expect(number("first_if")).toBe("1.1");
    expect(number("second_if")).toBe("1.2");
    expect(number("after")).toBe("2");
    // The full condition is on the Condition line; the selected tab names only its kind.
    expect(screen.getAllByText(/^Check the condition$/)).toHaveLength(1);
    fireEvent.click(screen.getByRole("tab", { name: /Otherwise/ }));
    expect(number("only_else")).toBe("1.3");
  });

  test("the side panel closes with its Close button and with Escape, and Details reopens it", () => {
    renderDoc(
      [
        block("extraction", "find_prices", null, {
          data_extraction_goal: "Get the prices",
        }),
      ],
      { apply: vi.fn(), update: vi.fn(), addStep: vi.fn(), duplicate: vi.fn() },
    );
    const toggle = screen.getByRole("button", {
      name: "Details for Find prices",
    });
    const openPanel = () => {
      fireEvent.click(toggle);
      fireEvent.click(
        screen.getByRole("button", { name: "More details in the side panel" }),
      );
      expect(
        screen.getByRole("complementary", { name: "Document panel" }),
      ).toBeTruthy();
    };
    openPanel();
    fireEvent.click(
      screen.getByRole("button", { name: "Close document panel" }),
    );
    expect(
      screen.queryByRole("complementary", { name: "Document panel" }),
    ).toBeNull();
    openPanel();
    fireEvent.keyDown(
      screen.getByRole("button", { name: "Close document panel" }),
      { key: "Escape" },
    );
    expect(
      screen.queryByRole("complementary", { name: "Document panel" }),
    ).toBeNull();
  });

  test("a step whose text is empty keeps its filled Terminate if in view with Details closed", () => {
    const nodes = renderDoc(
      [
        block("validation", "check", null, {
          complete_criterion: "",
          terminate_criterion: "Stop when the page is blocked",
        }),
      ],
      { apply: vi.fn(), update: vi.fn(), addStep: vi.fn(), duplicate: vi.fn() },
    );
    const step = within(
      document.querySelector<HTMLElement>(
        `[data-doc-node-id="${nodes.find((n) => n.data.label === "check")!.id}"]`,
      )!,
    );
    expect(
      step
        .getByRole("button", { name: /^Details for/ })
        .getAttribute("aria-expanded"),
    ).toBe("false");
    expect(step.getByText("Terminate if")).toBeTruthy();
    expect(
      step.getByRole("textbox", { name: "check terminateCriterion" }),
    ).toBeTruthy();
  });

  test("a step's open Details stays open after a keyboard move remounts it", () => {
    const doc = renderLiveDoc([
      block("text_prompt", "first", "second", {
        prompt: "Write A",
        parameters: [],
      }),
      block("text_prompt", "second", null, {
        prompt: "Write B",
        parameters: [],
      }),
    ]);
    const before = doc.graph();
    const second = before.nodes.find((node) => node.data.label === "second")!;
    const toggle = () =>
      within(
        document.querySelector<HTMLElement>(
          `[data-doc-node-id="${second.id}"]`,
        )!,
      ).getByRole("button", { name: /^Details for/ });
    fireEvent.click(toggle());
    fireEvent.click(screen.getByText("Write B"));
    fireEvent.keyDown(screen.getByRole("textbox", { name: "second prompt" }), {
      key: "ArrowUp",
      altKey: true,
      shiftKey: true,
    });
    expect(doc.graph()).not.toBe(before);
    doc.rerender();
    expect(toggle().getAttribute("aria-expanded")).toBe("true");
  });

  test("clearing a step's text leaves its other lines where they are until the line is left", () => {
    const doc = renderLiveDoc([
      block("task", "read_page", null, {
        navigation_goal: "Open the orders page",
        data_extraction_goal: "Get the order total",
        complete_criterion: "The total shows",
      }),
    ]);
    const extract = () =>
      screen.queryByRole("textbox", { name: "read_page dataExtractionGoal" });
    expect(extract()).toBeNull();
    const name = "read_page navigationGoal";
    fireEvent.focus(screen.getByRole("textbox", { name }));
    const box = screen.getByRole("textbox", { name });
    // jsdom has no innerText; the line reads it on input.
    Object.defineProperty(box, "innerText", { value: "", configurable: true });
    fireEvent.input(box);
    doc.rerender();
    expect(
      (
        doc.graph().nodes.find(isWorkflowBlockNode)!.data as Record<
          string,
          unknown
        >
      ).navigationGoal,
    ).toBe("");
    expect(extract()).toBeNull();
    // Focus moving within the line keeps the placement it started with.
    fireEvent.focus(screen.getByRole("textbox", { name }));
    expect(extract()).toBeNull();

    fireEvent.blur(screen.getByRole("textbox", { name }));
    expect(extract()).not.toBeNull();
  });

  test("a save lock during an edit drops the held placement, so the next edit starts from what the step shows", () => {
    const doc = renderLiveDoc([
      block("task", "read_page", null, {
        navigation_goal: "Open the orders page",
        data_extraction_goal: "Get the order total",
      }),
    ]);
    const extract = () =>
      screen.queryByRole("textbox", { name: "read_page dataExtractionGoal" });
    const name = "read_page navigationGoal";
    fireEvent.focus(screen.getByRole("textbox", { name }));
    const box = screen.getByRole("textbox", { name });
    Object.defineProperty(box, "innerText", { value: "", configurable: true });
    fireEvent.input(box);
    doc.rerender();
    expect(extract()).toBeNull();
    act(() => useWorkflowYamlEditorStore.setState({ commitInProgress: true }));
    act(() => useWorkflowYamlEditorStore.setState({ commitInProgress: false }));
    fireEvent.focus(screen.getByRole("textbox", { name }));
    expect(extract()).not.toBeNull();
  });

  test("a step shows its type, title and text; Details lists its custom settings by name and value, and its last item opens the side panel", () => {
    const nodes = renderDoc(
      [
        block("extraction", "find_prices", null, {
          data_extraction_goal: "Get the prices",
          max_retries: 3,
          max_steps_per_run: 12,
        }),
      ],
      { apply: vi.fn(), update: vi.fn(), addStep: vi.fn(), duplicate: vi.fn() },
    );
    const line = document.querySelector<HTMLElement>(
      `[data-doc-node-id="${nodes.find(isWorkflowBlockNode)!.id}"]`,
    )!;
    expect(line.textContent).toContain("Get the prices");
    expect(line.textContent).not.toMatch(/Max retries|Max steps|Saves|Uses/);
    const toggle = screen.getByRole("button", {
      name: "Details for Find prices",
    });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    const options = within(
      document.getElementById(toggle.getAttribute("aria-controls")!)!,
    );
    expect(options.getByText("Custom settings")).toBeTruthy();
    expect(options.getByText("Max steps").nextElementSibling?.textContent).toBe(
      "12",
    );
    expect(
      options.getByText("Max retries").nextElementSibling?.textContent,
    ).toBe("3");
    // Settings at their default appear only in the side panel.
    expect(options.queryByText("Continue on failure")).toBeNull();

    fireEvent.click(
      options.getByRole("button", { name: "More details in the side panel" }),
    );
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    const selected = within(screen.getByLabelText("Selected step"));
    expect(selected.getByText("Find prices")).toBeTruthy();
    expect(selected.getByText("find_prices")).toBeTruthy();
  });

  test("every step has Details, a form with none of the listed settings lists none, and a read-only Doc lists the settings too", () => {
    const nodes = renderDoc(
      [
        block("wait", "pause", "open_site", { wait_sec: 5 }),
        block("goto_url", "open_site", "save_page", {
          url: "https://example.com",
        }),
        block("print_page", "save_page", "query_sales", {}),
        block("powerbi_query", "query_sales", "stop_here", {
          query: "EVALUATE Sales",
        }),
        block("terminate", "stop_here", null, { reason: "Done" }),
      ],
      { apply: vi.fn(), update: vi.fn(), addStep: vi.fn(), duplicate: vi.fn() },
    );
    expect(nodes.filter(isWorkflowBlockNode).map((n) => n.type)).toEqual([
      "wait",
      "url",
      "printPage",
      "powerBIQuery",
      "terminate",
    ]);
    const details = screen.getAllByRole("button", { name: /^Details for/ });
    expect(details).toHaveLength(5);
    fireEvent.click(details[0]!);
    const box = within(
      document.getElementById(details[0]!.getAttribute("aria-controls")!)!,
    );
    expect(box.queryByRole("term")).toBeNull();
    expect(screen.queryByText(/Continue on failure|Model:/)).toBeNull();

    cleanup();
    renderDoc([
      block("extraction", "find_prices", null, { max_steps_per_run: 12 }),
    ]);
    const readOnly = screen.getByRole("button", {
      name: "Details for Find prices",
    });
    fireEvent.click(readOnly);
    const readOnlyBox = within(
      document.getElementById(readOnly.getAttribute("aria-controls")!)!,
    );
    expect(
      readOnlyBox.getByText("Max steps").nextElementSibling?.textContent,
    ).toBe("12");
    // A read-only Doc's side panel cannot show the settings, so Details does not point there.
    expect(
      readOnlyBox.queryByRole("button", {
        name: "More details in the side panel",
      }),
    ).toBeNull();
  });

  test("Details lists a step's declared inputs and its {{ }} reads as Inputs, matching the input chips, and never a Code step's Python", () => {
    useWorkflowParametersStore.setState({
      parameters: [
        {
          key: "site_login",
          parameterType: "credential",
          dataType: WorkflowParameterValueType.CredentialId,
          credentialId: "cred_1",
          description: null,
        },
        {
          key: "account_id",
          parameterType: "workflow",
          dataType: WorkflowParameterValueType.String,
          description: null,
          defaultValue: "a1",
        },
        {
          key: "unused_input",
          parameterType: "workflow",
          dataType: WorkflowParameterValueType.String,
          description: null,
          defaultValue: "x",
        },
      ] as never,
    });
    const nodes = renderDoc(
      [
        block("login", "sign_in", "count_rows", {
          url: "https://example.com/login",
          navigation_goal: "Sign in",
          complete_criterion: "The account {{ account_id }} page shows",
          parameters: [{ key: "site_login" }],
        }),
        block("code", "count_rows", null, {
          parameters: [],
          code: 'result = f"{{\\"count\\": {len(rows)}}}"',
        }),
      ],
      { apply: vi.fn(), update: vi.fn(), addStep: vi.fn(), duplicate: vi.fn() },
    );
    const step = (label: string) =>
      within(
        document.querySelector<HTMLElement>(
          `[data-doc-node-id="${nodes.find((n) => n.data.label === label)!.id}"]`,
        )!,
      );
    const details = (label: string, title: string) => {
      expect(step(label).queryByText("Inputs")).toBeNull();
      const toggle = step(label).getByRole("button", {
        name: `Details for ${title}`,
      });
      fireEvent.click(toggle);
      return within(
        document.getElementById(toggle.getAttribute("aria-controls")!)!,
      );
    };
    // A step's secondary lines (Complete if) wait in Details, as its inputs do.
    expect(step("sign_in").queryByText("Complete if")).toBeNull();
    const inputs = details("sign_in", "Sign in").getByText("Inputs");
    expect(step("sign_in").getByText("Complete if")).toBeTruthy();
    expect(
      Array.from(
        inputs.nextElementSibling!.querySelectorAll("span"),
        (pill) => pill.textContent,
      ),
    ).toEqual(["site_login", "account_id"]);
    expect(
      details("count_rows", "Count rows").queryByText("Inputs"),
    ).toBeNull();
    const chip = (key: string) =>
      screen.getByRole("button", { name: new RegExp(`^Input ${key}`) });
    expect(chip("site_login").getAttribute("aria-label")).not.toMatch(
      /not used/,
    );
    expect(chip("account_id").getAttribute("aria-label")).not.toMatch(
      /not used/,
    );
    expect(chip("unused_input").getAttribute("aria-label")).toMatch(/not used/);
  });

  test("Details lists the output fields of a jsonSchema step and of an extraction that returns a list of rows", () => {
    renderDoc(
      [
        block("text_prompt", "summarize_page", "list_orders", {
          prompt: "Summarize the page",
          json_schema: {
            type: "object",
            properties: {
              title: { type: "string" },
              price: { type: "number" },
            },
          },
        }),
        block("extraction", "list_orders", null, {
          data_extraction_goal: "Get every order",
          data_schema: {
            type: "array",
            items: {
              type: "object",
              properties: {
                order_id: { type: "string" },
                total: { type: "number" },
              },
            },
          },
        }),
      ],
      { apply: vi.fn(), update: vi.fn(), addStep: vi.fn(), duplicate: vi.fn() },
    );
    const fields = (title: string) => {
      const toggle = screen.getByRole("button", {
        name: `Details for ${title}`,
      });
      fireEvent.click(toggle);
      return within(
        document.getElementById(toggle.getAttribute("aria-controls")!)!,
      ).getByText(/^Fields:/).textContent;
    };
    expect(fields("Summarize page")).toBe("Fields: title, price");
    expect(fields("List orders")).toBe("Fields: order_id, total");
  });
});

test("a problem's Jump opens Details when the field it is about sits there", async () => {
  vi.useFakeTimers();
  const post = vi.fn().mockResolvedValue({
    data: {
      valid: true,
      problems: [
        {
          code: "unknown_reference",
          severity: "error",
          block_label: "read_page",
          field: "complete_criterion",
          message: "Unknown reference: missing_value.",
        },
      ],
    },
  });
  vi.spyOn(axiosClientModule, "getClient").mockResolvedValue({
    post,
  } as never);
  const getSaveData = useWorkflowHasChangesStore.getState().getSaveData;
  useWorkflowHasChangesStore.setState({ getSaveData: () => ({}) as never });
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
  vi.stubGlobal("CSS", { escape: (value: string) => value });
  try {
    renderDoc([
      block("task", "read_page", null, {
        navigation_goal: "Open the page",
        complete_criterion: "{{ missing_value }} shows",
      }),
    ]);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    const details = () =>
      screen
        .getByRole("button", { name: /^Details for/ })
        .getAttribute("aria-expanded");
    expect(details()).toBe("false");
    fireEvent.click(
      document.querySelector<HTMLElement>("[data-doc-problems]")!,
    );
    fireEvent.click(
      within(screen.getByRole("dialog")).getByText(
        /Unknown reference: missing_value/,
      ),
    );
    expect(details()).toBe("true");
  } finally {
    useWorkflowHasChangesStore.setState({ getSaveData });
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    cleanup();
  }
});

test("a step that reads an output only some branches produce shows the warning under it", async () => {
  vi.useFakeTimers();
  const post = vi.fn().mockResolvedValue({
    data: {
      valid: true,
      problems: [
        {
          code: "branch_only_reference",
          severity: "warning",
          block_label: "inner_step",
          message: "left_output is only produced on some branches of check.",
        },
        {
          code: "forward_reference",
          severity: "error",
          block_label: "inner_step",
          message: "later_output is produced by a later step.",
        },
        {
          code: "unknown_reference",
          severity: "error",
          block_label: "inner_step",
          message: "Unknown reference: missing_value.",
        },
      ],
    },
  });
  vi.spyOn(axiosClientModule, "getClient").mockResolvedValue({
    post,
  } as never);
  const getSaveData = useWorkflowHasChangesStore.getState().getSaveData;
  useWorkflowHasChangesStore.setState({ getSaveData: () => ({}) as never });
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
  try {
    renderDoc();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    const stepProblem = screen.getByText(
      /left_output is only produced on some branches of check\./,
    );
    expect(stepProblem.textContent).toContain(
      "later_output is produced by a later step.",
    );
    expect(stepProblem.textContent).toContain(
      "Unknown reference: missing_value.",
    );
    // A warning on a step is not a note that needs fixing.
    expect(screen.queryByText(/needs? fixing/)).toBeNull();
  } finally {
    useWorkflowHasChangesStore.setState({ getSaveData });
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    cleanup();
  }
});

describe("a step that was just added", () => {
  beforeEach(() => {
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
    useWorkflowParametersStore.setState({ parameters: [] });
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    );
    vi.stubGlobal("CSS", { escape: (value: string) => value });
  });
  afterEach(() => {
    cleanup();
    useDocUnfinishedStore.getState().clear();
    vi.unstubAllGlobals();
  });

  test("shows its missing setting once, and only after the user leaves it", () => {
    const nodes = renderDoc([block("login", "sign_in", null, { url: "" })]);
    const login = nodes.find((n) => isWorkflowBlockNode(n))!;
    act(() => useDocUnfinishedStore.getState().add(login.id));
    expect(document.querySelector("[data-doc-step-fix]")).toBeNull();
    expect(document.querySelector("[data-doc-problems]")).toBeNull();
    expect(blockingCount()).toBe(0);

    fireEvent.pointerDown(document.body);

    expect(screen.getAllByText(docDetailsText.noCredential(true))).toHaveLength(
      1,
    );
    expect(document.querySelector("[data-doc-problems]")?.textContent).toBe(
      "1 problem",
    );
    expect(screen.queryByText(/need.* fixing/)).toBeNull();
  });

  test("a read-only Doc's credential line does not send the user to the side panel, and its Jump leaves Details closed", async () => {
    useWorkflowYamlEditorStore.setState({ commitInProgress: true });
    renderDoc([block("login", "sign_in", null, { url: "" })]);
    expect(screen.getByText(docDetailsText.noCredential(false))).toBeTruthy();
    expect(screen.queryByText(/side panel to choose/)).toBeNull();
    fireEvent.click(
      document.querySelector<HTMLElement>("[data-doc-problems]")!,
    );
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByText(
        /no saved credential/,
      ),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(
      screen
        .getByRole("button", { name: /^Details for/ })
        .getAttribute("aria-expanded"),
    ).toBe("false");
  });

  test.each([
    ["no list", null, true],
    ["an empty reference", "{{ }}", true],
    ["a blank reference", "{{   }}", true],
    ["an earlier step's output list", "{{ search_output.results }}", false],
    ["a parameter list", "{{ topics }}", false],
    ["a literal list", '["a", "b"]', false],
  ])(
    "a loop line with %s shows the missing-list problem in place of its summary only when it names no list",
    (_, list, noList) => {
      renderDoc([
        forLoop("each_result", null, [code("open_result", null)], {
          loop_over: null,
          loop_variable_reference: list,
          description: "For each of the search results",
        }),
      ]);
      expect(screen.getByText("For each of the search results")).toBeTruthy();
      expect(screen.queryByText("For each item")).toBeNull();
      const fix =
        document.querySelector("[data-doc-step-fix]")?.textContent ?? "";
      expect(fix.includes("the loop needs the list itself")).toBe(noList);
    },
  );

  test.each([
    ["{{ }}", "a stale list behind it", { key: "stale_topics" }],
    ["{{ }}", "nothing behind it", null],
    ["  ", "a stale list behind it", { key: "stale_topics" }],
  ])(
    "a loop whose reference is %j with %s has no list and asks before Run",
    (reference, _, loopOver) => {
      renderDoc([
        forLoop("each_result", null, [code("open_result", null)], {
          loop_over: loopOver,
          loop_variable_reference: reference,
        }),
      ]);
      expect(document.querySelector("[data-doc-step-fix]")).toBeTruthy();
      expect(blockingCount()).toBeGreaterThan(0);
      expect(screen.queryByText(/stale_topics/)).toBeNull();
    },
  );

  test("a new Go to URL step shows no URL-required chip until the user leaves it", () => {
    const nodes = renderDoc([
      block("goto_url", "open_site", null, { url: "" }),
    ]);
    act(() =>
      useDocUnfinishedStore.getState().add(nodes.find(isWorkflowBlockNode)!.id),
    );
    expect(document.querySelector("[data-doc-missing]")).toBeNull();

    fireEvent.pointerDown(document.body);

    expect(document.querySelector('[data-doc-missing="URL"]')).toBeTruthy();
  });

  test("a loop over a whole extraction result is a problem; the typed list path is not", () => {
    const steps = (reference: string) => [
      block("extraction", "extraction_1", "books", {
        data_extraction_goal: "List the titles",
        data_schema: null,
      }),
      forLoop("books", null, [block("navigation", "open_book", null)], {
        loop_over: null,
        loop_variable_reference: reference,
      }),
    ];
    renderDoc(steps("{{ extraction_1_output }}"), {
      apply: vi.fn(),
      update: vi.fn(),
      addStep: vi.fn(() => "new_step"),
      duplicate: vi.fn(),
    });
    expect(
      screen.getByText(
        /^extraction_1 has no top-level list in its Data Schema/,
      ),
    ).toBeTruthy();
    expect(blockingCount()).toBe(1);
    fireEvent.click(
      screen.getByRole("button", { name: "Open extraction_1's Data Schema" }),
    );
    expect(
      within(screen.getByLabelText("Selected step")).getByText("extraction_1"),
    ).toBeTruthy();
    cleanup();
    renderDoc(steps("{{ extraction_1_output.extracted_information.titles }}"));
    expect(
      screen.queryByText(/has no top-level list in its Data Schema/),
    ).toBeNull();
    expect(blockingCount()).toBe(0);
  });

  test("the problems button lists every problem it counts, and jumps to the step of the one picked", async () => {
    // jsdom has no scrollIntoView, so vi.spyOn has nothing to wrap; the original is put back after the test.
    const original = Element.prototype.scrollIntoView;
    const scroll = vi.fn();
    Element.prototype.scrollIntoView = scroll;
    onTestFinished(() => {
      Element.prototype.scrollIntoView = original;
    });
    const nodes = renderDoc([
      block("login", "sign_in", "each_result", { url: "" }),
      forLoop(
        "each_result",
        "open_site",
        [block("navigation", "open_result", null)],
        {
          loop_over: null,
          loop_variable_reference: null,
        },
      ),
      block("goto_url", "open_site", null, { url: "" }),
    ]);
    const trigger = document.querySelector<HTMLElement>("[data-doc-problems]")!;
    expect(trigger.textContent).toBe("3 problems");
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(trigger);
    const list = await screen.findByRole("dialog", { name: "3 problems" });
    const entries = within(list).getAllByRole("button");
    expect(entries).toHaveLength(3);
    expect(entries.map((entry) => entry.textContent)).toEqual([
      expect.stringMatching(/Login.*no saved credential/),
      expect.stringMatching(/Loop.*no list to go through/),
      expect.stringMatching(/Go to URL.*no URL/),
    ]);
    fireEvent.click(entries[2]!);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    const site = nodes.find((node) => node.data.label === "open_site")!;
    await waitFor(() =>
      expect(scroll.mock.contexts[0]).toBe(
        document.querySelector(`[data-doc-node-id="${site.id}"]`),
      ),
    );
    const details = (label: string) =>
      within(
        document.querySelector<HTMLElement>(
          `[data-doc-node-id="${nodes.find((node) => node.data.label === label)!.id}"]`,
        )!,
      )
        .getAllByRole("button", { name: /^Details for/ })[0]!
        .getAttribute("aria-expanded");
    // The URL is on the step's line; the credential is chosen from Details, so only that jump opens them.
    expect(details("open_site")).toBe("false");
    fireEvent.click(trigger);
    fireEvent.click(
      within(
        await screen.findByRole("dialog", { name: "3 problems" }),
      ).getAllByRole("button")[0]!,
    );
    await waitFor(() => expect(details("sign_in")).toBe("true"));
  });

  test("Fix problems from Run's question opens the list, and Escape returns focus to the line Run was pressed from", async () => {
    const nodes = renderDoc(
      [
        block("goto_url", "open_site", "find_it", { url: "" }),
        block("navigation", "find_it", null, {
          navigation_goal: "Find the price",
          complete_criterion: "The price is shown",
        }),
      ],
      { apply: vi.fn(), update: vi.fn(), addStep: vi.fn(), duplicate: vi.fn() },
    );
    const step = nodes.find((node) => node.data.label === "find_it")!;
    const second = "find_it completeCriterion";
    fireEvent.click(
      screen.getByRole("button", { name: "Details for Find it" }),
    );
    act(() => screen.getByRole("textbox", { name: second }).focus());
    const point = captureReturnPoint(document.activeElement);
    expect(point).toMatchObject({ stepId: step.id, lineLabel: second });
    // The Run question had focus when its Fix problems was pressed; the line's edit span is gone by then.
    act(() => (document.activeElement as HTMLElement).blur());
    act(() => {
      window.dispatchEvent(
        new CustomEvent<DocShowProblemsDetail>(DOC_SHOW_PROBLEMS_EVENT, {
          detail: { returnPoint: point },
        }),
      );
    });
    await screen.findByRole("dialog", { name: "1 problem" });
    fireEvent.keyDown(document.activeElement ?? document.body, {
      key: "Escape",
    });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() =>
      expect(document.activeElement?.getAttribute("aria-label")).toBe(second),
    );
  });

  test("a freshly added loop over a picked whole result is a problem once the user leaves it", () => {
    const nodes = renderDoc([
      block("extraction", "extraction_1", "books", {
        data_extraction_goal: "List the titles",
        data_schema: null,
      }),
      forLoop("books", null, [block("navigation", "open_book", null)], {
        loop_over: null,
        loop_variable_reference: "{{extraction_1_output}}",
      }),
    ]);
    const loop = nodes.find((n) => n.type === "loop")!;
    act(() => useDocUnfinishedStore.getState().add(loop.id));
    expect(
      screen.queryByText(/has no top-level list in its Data Schema/),
    ).toBeNull();
    expect(blockingCount()).toBe(0);

    fireEvent.pointerDown(document.body);

    expect(
      screen.getByText(
        /^extraction_1 has no top-level list in its Data Schema/,
      ),
    ).toBeTruthy();
    expect(blockingCount()).toBe(1);
  });

  test("a click inside a floating menu does not count as leaving the new step", () => {
    const nodes = renderDoc([block("login", "sign_in", null, { url: "" })]);
    const login = nodes.find((n) => isWorkflowBlockNode(n))!;
    act(() => useDocUnfinishedStore.getState().add(login.id));
    const option = document.body.appendChild(document.createElement("div"));
    option.setAttribute("role", "listbox");
    fireEvent.pointerDown(option);
    expect(document.querySelector("[data-doc-problems]")).toBeNull();
    option.remove();
  });
});

describe("A Browser Action that needs several actions", () => {
  const actionStep = {
    label: "search_step",
    block_type: "action",
    next_block_label: null,
    continue_on_failure: false,
    model: null,
    output_parameter: output("search_step"),
    url: "",
    navigation_goal: "Search for apple stock and get its price",
    parameters: [],
  } as unknown as WorkflowBlock;

  beforeEach(() => {
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
    useWorkflowParametersStore.setState({ parameters: [] });
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    );
    Element.prototype.scrollIntoView = vi.fn();
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  test("offers to turn into a Browser Task before running, keeping the text", () => {
    const apply = vi.fn();
    renderDoc([actionStep], {
      apply,
      update: vi.fn(),
      addStep: vi.fn(() => null),
      duplicate: vi.fn(),
    });

    fireEvent.click(
      screen.getByRole("button", { name: "Turn into Browser Task" }),
    );

    const turned = apply.mock.calls[0]![0].nodes.find(
      (node: { data: { label?: string } }) => node.data.label === "search_step",
    );
    expect(turned.type).toBe("navigation");
    expect(turned.data.navigationGoal).toBe(
      "Search for apple stock and get its price",
    );
  });
});

describe("Doc inputs", () => {
  beforeEach(() => {
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    );
    useWorkflowParametersStore.setState({
      parameters: [
        {
          key: "query",
          parameterType: "workflow",
          dataType: WorkflowParameterValueType.String,
          description: null,
          defaultValue: "cats",
        },
      ],
    });
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  test("an input added, renamed and deleted in the Doc is the workflow's parameter and its mentions follow", () => {
    let graph = getElements(
      [
        block("text_prompt", "summarize", "each_result", {
          prompt: "Search for {{ query }}",
          parameters: [{ key: "query" }],
        }),
        forLoop("each_result", null, [code("open_result", null)], {
          loop_over: { key: "query" },
          loop_variable_reference: "query",
        }),
      ],
      settings(),
      true,
    ) as DocGraph;
    const client = new QueryClient();
    const edits: DocGraphEdits = {
      apply: () => {},
      update: (change) => {
        graph = { ...graph, nodes: change(graph.nodes) };
      },
      addStep: () => null,
      duplicate: () => {},
    };
    const view = () => (
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <ReactFlowProvider>
            <WorkflowDocEditor {...graph} edits={edits} />
          </ReactFlowProvider>
        </MemoryRouter>
      </QueryClientProvider>
    );
    const rendered = render(view());
    const inPopover = (name: string) =>
      within(screen.getByRole("dialog")).getByRole("button", { name });
    const keys = () =>
      useWorkflowParametersStore.getState().parameters.map((p) => p.key);
    const loopSource = () =>
      (
        graph.nodes.find((n) => n.type === "loop")!.data as {
          loopValue: string;
        }
      ).loopValue;
    const prompt = () =>
      graph.nodes.find((n) => n.data.label === "summarize")!.data as {
        prompt: string;
        parameterKeys: string[] | null;
      };

    useWorkflowHasChangesStore.getState().setHasChanges(false);
    fireEvent.click(screen.getByRole("button", { name: "+ Input" }));
    expect(keys()).toContain("new_input");
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    // Closing an untouched new input leaves the agent as it was.
    expect(keys()).toEqual(["query"]);
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "+ Input" }));
    // A just-created placeholder is still an Add: not titled "Edit", and no "changing the type" warning.
    expect(screen.getByText("Add Input")).toBeTruthy();
    expect(screen.getByRole("dialog").getAttribute("aria-label")).toBe(
      "Add input",
    );
    fireEvent.change(
      within(screen.getByRole("dialog")).getAllByRole("textbox")[0]!,
      {
        target: { value: "topic" },
      },
    );
    fireEvent.click(inPopover("Save"));
    expect(useWorkflowParametersStore.getState().parameters[1]).toEqual({
      key: "topic",
      parameterType: "workflow",
      dataType: WorkflowParameterValueType.String,
      description: "",
      defaultValue: null,
    });
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: /^Input query/ }));
    fireEvent.change(
      within(screen.getByRole("dialog")).getAllByRole("textbox")[0]!,
      {
        target: { value: "search" },
      },
    );
    fireEvent.click(inPopover("Save"));
    expect(keys()).toEqual(["search", "topic"]);
    expect(prompt().prompt).toBe("Search for {{ search }}");
    expect(prompt().parameterKeys).toEqual(["search"]);
    expect(loopSource()).toBe("search");

    // The Doc no longer offers a context input; one saved earlier still shows, opens and blocks deleting its source.
    expect(screen.queryByRole("button", { name: /context input/i })).toBeNull();
    useWorkflowParametersStore.setState({
      parameters: [
        ...useWorkflowParametersStore.getState().parameters,
        {
          key: "derived",
          parameterType: "context",
          sourceParameterKey: "search",
          description: null,
        },
      ],
    });
    rendered.rerender(view());
    const derived = screen.getByRole("button", { name: /^Input derived/ });
    expect(derived.getAttribute("title")).toMatch(/^Uses data from search/);
    fireEvent.click(derived);
    expect(screen.getByText("Edit Context Input")).toBeTruthy();
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });

    fireEvent.click(screen.getByRole("button", { name: /^Input search/ }));
    fireEvent.click(inPopover("Delete input"));
    expect(keys()).toEqual(["search", "topic", "derived"]);
    fireEvent.click(screen.getByRole("button", { name: /^Input derived/ }));
    fireEvent.click(inPopover("Delete input"));
    fireEvent.click(screen.getByRole("button", { name: /^Input search/ }));
    fireEvent.click(inPopover("Delete input"));
    expect(keys()).toEqual(["topic"]);
    expect(prompt().prompt).toBe("Search for {{ search }}");
    expect(prompt().parameterKeys).toEqual([]);
    rendered.rerender(view());
    expect(screen.getByTitle("{{ search }}: missing reference")).toBeTruthy();
  });
});

describe("Demonstrate task in the step menu", () => {
  const edits: DocGraphEdits = {
    apply: vi.fn(),
    update: vi.fn(),
    addStep: vi.fn(() => null),
    duplicate: vi.fn(),
  };
  const start = vi.fn();
  const twoSteps = [
    code("first_step", "second_step"),
    code("second_step", null),
  ];
  const emptyLoop = {
    ...loop,
    loop_blocks: [],
  } as unknown as WorkflowBlock;

  beforeEach(() => {
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
    useWorkflowParametersStore.setState({ parameters: [] });
    useRecordingLauncherStore.setState({
      startDemonstration: start,
      demonstration: null,
    });
    start.mockClear();
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    );
    vi.stubGlobal("requestAnimationFrame", (cb: () => void) => {
      cb();
      return 0;
    });
    Element.prototype.scrollIntoView = vi.fn();
  });
  afterEach(() => {
    cleanup();
    useRecordingLauncherStore.setState({
      startDemonstration: null,
      demonstration: null,
    });
    vi.unstubAllGlobals();
  });

  test("dragging a step above its neighbour says it moved up, not the list's name", () => {
    clearDocAnnouncement();
    const edits: DocGraphEdits = {
      apply: vi.fn(),
      update: vi.fn(),
      addStep: vi.fn(() => null),
      duplicate: vi.fn(),
    };
    renderDoc(twoSteps, edits);
    const grip = screen.getByRole("button", {
      name: "Actions for second_step",
    });
    fireEvent.dragStart(grip, {
      dataTransfer: {
        setData: () => {},
        setDragImage: () => {},
        effectAllowed: "",
      },
    });
    const zone = document.querySelector<HTMLElement>(
      "div[aria-hidden].h-3.-my-3",
    );
    expect(zone).toBeTruthy();
    fireEvent.drop(zone!, { dataTransfer: {} });
    expect(docAnnouncement()).toBe("Moved up");
  });

  test("the + between steps offers no Demonstrate task, with or without a note above the step", async () => {
    renderDoc(twoSteps, edits);
    fireEvent.click(
      screen.getAllByRole("button", { name: "Add a step here" })[0]!,
    );
    expect(await screen.findByPlaceholderText("Add a step…")).toBeTruthy();
    expect(screen.queryByText("Demonstrate task")).toBe(null);
  });

  test("Escape on the + between steps, with a note above the step, puts focus back on that +", async () => {
    renderDoc(
      [
        code("first_step", "second_step"),
        { ...code("second_step", null), notes: "Check the totals" },
      ],
      edits,
    );
    const plus = screen.getAllByRole("button", { name: "Add a step here" })[0]!;
    fireEvent.click(plus);
    const search = await screen.findByPlaceholderText("Add a step…");
    fireEvent.keyDown(search, { key: "Escape" });
    await waitFor(() => expect(document.activeElement).toBe(plus));
  });

  test("Add step records after the last step, keeps the Doc open, and the item is searchable", () => {
    renderDoc(twoSteps, edits);
    fireEvent.click(screen.getByRole("button", { name: "+ Add step" }));

    expect(screen.getByText("Copilot")).toBeTruthy();
    expect(screen.getByText("Show Copilot how in the browser")).toBeTruthy();
    fireEvent.change(screen.getByPlaceholderText("Add a step…"), {
      target: { value: "demo" },
    });
    expect(screen.queryByText("No step matches.")).toBe(null);

    fireEvent.click(screen.getByText("Demonstrate task"));
    expect(start).toHaveBeenCalledWith({
      insertion: { after_label: "second_step", before_label: null },
      keepEditor: true,
    });
  });

  test("the item is left out without a live browser and inside a loop", () => {
    useRecordingLauncherStore.setState({ startDemonstration: null });
    renderDoc(twoSteps, edits);
    fireEvent.click(screen.getByRole("button", { name: "+ Add step" }));
    expect(screen.queryByText("Demonstrate task")).toBe(null);
    cleanup();

    useRecordingLauncherStore.setState({ startDemonstration: start });
    renderDoc([emptyLoop], edits);
    fireEvent.click(screen.getByRole("button", { name: "+ Add a step" }));
    expect(screen.queryByText("Demonstrate task")).toBe(null);
  });

  test("the Doc holds still while a recording runs, so its anchor steps cannot move", () => {
    useRecordingLauncherStore.setState({
      demonstration: {
        phase: "recording",
        actionCount: 1,
        interrupted: false,
        insertion: { after_label: "first_step", before_label: "second_step" },
      },
    });
    renderDoc(twoSteps, edits);

    expect(screen.queryByRole("button", { name: "+ Add step" })).toBe(null);
  });

  test("a running recording shows one status line between the steps it will land between", () => {
    useRecordingLauncherStore.setState({
      demonstration: {
        phase: "recording",
        actionCount: 2,
        interrupted: false,
        insertion: { after_label: "first_step", before_label: "second_step" },
      },
    });
    renderDoc(twoSteps, edits);

    const line = screen.getByText(/^Demonstrating · 2 actions$/);
    expect(line.textContent).toBe("Demonstrating · 2 actions");
    const first = screen.getByText("First step");
    const second = screen.getByText("Second step");
    expect(
      first.compareDocumentPosition(line) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      second.compareDocumentPosition(line) & Node.DOCUMENT_POSITION_PRECEDING,
    ).toBeTruthy();
  });
  describe("from an empty step", () => {
    const blankBetween = [
      block("goto_url", "first_step", "blank_step", { url: "https://a.test" }),
      block("goto_url", "blank_step", "second_step", { url: "" }),
      code("second_step", null),
    ];
    const recording = (phase: "recording" | "stopped") => ({
      phase,
      actionCount: 0,
      interrupted: false,
      insertion: { after_label: "first_step", before_label: "second_step" },
    });
    beforeEach(() => {
      vi.stubGlobal("CSS", { escape: (value: string) => value });
    });
    // jsdom has no innerText; the line reads its text through it.
    const openLine = (name: string, text: string, at: number) => {
      fireEvent.focus(screen.getByRole("textbox", { name }));
      const box = screen.getByRole("textbox", { name });
      Object.defineProperty(box, "innerText", {
        value: text,
        configurable: true,
      });
      box.textContent = text;
      const range = document.createRange();
      if (box.firstChild) range.setStart(box.firstChild, at);
      else range.setStart(box, 0);
      range.collapse(true);
      window.getSelection()?.removeAllRanges();
      window.getSelection()?.addRange(range);
      return box;
    };
    const startFromBlank = () => {
      const applied = {
        apply: vi.fn(),
        update: vi.fn(),
        addStep: vi.fn(() => null),
        duplicate: vi.fn(),
      } as DocGraphEdits;
      renderDoc(blankBetween, applied);
      fireEvent.keyDown(openLine("blank_step url", "", 0), { key: "/" });
      fireEvent.click(screen.getByText("Demonstrate task"));
      return applied;
    };

    test("records between its neighbours and drops the step only once the recording is open", () => {
      const applied = startFromBlank();

      expect(start).toHaveBeenCalledWith({
        insertion: { after_label: "first_step", before_label: "second_step" },
        keepEditor: true,
      });
      expect(applied.apply).not.toHaveBeenCalled();

      act(() =>
        useRecordingLauncherStore.setState({
          demonstration: recording("recording"),
        }),
      );
      const kept = vi.mocked(applied.apply).mock.calls[0]![0].nodes;
      expect(kept.filter(isWorkflowBlockNode).map((n) => n.data.label)).toEqual(
        ["first_step", "second_step"],
      );
    });

    test("a start that fails or is refused leaves the step, even for a later recording in the same place", () => {
      const applied = startFromBlank();

      act(() => noteDemonstrationStartFailed());
      act(() =>
        useRecordingLauncherStore.setState({
          demonstration: recording("recording"),
        }),
      );
      expect(applied.apply).not.toHaveBeenCalled();
    });

    test("a slow start that opens after 25 seconds still replaces the step", () => {
      vi.useFakeTimers();
      try {
        const applied = startFromBlank();

        act(() => {
          vi.advanceTimersByTime(25_000);
        });
        expect(applied.apply).not.toHaveBeenCalled();
        act(() =>
          useRecordingLauncherStore.setState({
            demonstration: recording("recording"),
          }),
        );
        const kept = vi.mocked(applied.apply).mock.calls[0]![0].nodes;
        expect(
          kept.filter(isWorkflowBlockNode).map((n) => n.data.label),
        ).toEqual(["first_step", "second_step"]);
      } finally {
        vi.useRealTimers();
      }
    });

    test("a recording that stops before it is open leaves the step in place", () => {
      const applied = startFromBlank();

      act(() =>
        useRecordingLauncherStore.setState({
          demonstration: recording("stopped"),
        }),
      );
      act(() =>
        useRecordingLauncherStore.setState({
          demonstration: recording("recording"),
        }),
      );
      expect(applied.apply).not.toHaveBeenCalled();
    });
  });
});

describe("the Add note button", () => {
  beforeEach(() => {
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
    useWorkflowParametersStore.setState({ parameters: [] });
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    );
    vi.stubGlobal("requestAnimationFrame", (cb: () => void) => {
      cb();
      return 0;
    });
    vi.stubGlobal("CSS", { escape: (value: string) => value });
    Element.prototype.scrollIntoView = vi.fn();
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  const steps = () => [
    block("wait", "open_site", "read_page", { wait_sec: 5 }),
    block("navigation", "read_page", null, {
      navigation_goal: "Read the page",
    }),
  ];
  const typeNote = (text: string) => {
    const note = screen.getByRole("textbox", { name: "Note" });
    expect(document.activeElement).toBe(note);
    Object.defineProperty(note, "innerText", {
      value: text,
      configurable: true,
    });
    fireEvent.input(note);
  };
  const notesOf = (doc: ReturnType<typeof renderLiveDoc>) =>
    Object.fromEntries(
      doc
        .graph()
        .nodes.filter(isWorkflowBlockNode)
        .map((n) => {
          const data = n.data as Record<string, unknown>;
          return [n.data.label, [data.notes ?? "", data.trailingNotes ?? ""]];
        }),
    );
  const addNote = (step: string) =>
    screen.getByRole("button", { name: `Add note below ${step}` });
  const addNoteButtons = () =>
    screen.queryAllByRole("button", { name: /^Add note below / });
  const firstLine = () => screen.getByRole("textbox", { name: /^open_site / });

  test("is on every step without hovering, and saves the same note as Enter at the end of a step", () => {
    const byEnter = renderLiveDoc(steps());
    expect(addNoteButtons().map((b) => b.getAttribute("aria-label"))).toEqual([
      "Add note below Open site",
      "Add note below Read page",
    ]);
    fireEvent.focus(firstLine());
    fireEvent.keyDown(firstLine(), { key: "Enter" });
    typeNote("Check the total");
    const expected = notesOf(byEnter);
    cleanup();

    const byButton = renderLiveDoc(steps());
    fireEvent.click(addNote("Open site"));
    typeNote("Check the total");
    expect(notesOf(byButton)).toEqual(expected);
    expect(expected.read_page).toEqual(["Check the total", ""]);
  });

  test("after the last step it adds the note that trails it", () => {
    const doc = renderLiveDoc(steps());
    fireEvent.click(addNote("Read page"));
    typeNote("Done here");
    expect(notesOf(doc).read_page).toEqual(["", "Done here"]);
  });

  test("where a note already follows the step it goes to that note", () => {
    const doc = renderLiveDoc([
      steps()[0]!,
      block("navigation", "read_page", null, {
        navigation_goal: "Read the page",
        notes: "Look closely",
      }),
    ]);
    fireEvent.click(addNote("Open site"));
    expect(screen.getAllByRole("textbox", { name: "Note" })).toHaveLength(1);
    expect(document.activeElement).toBe(
      screen.getByRole("textbox", { name: "Note" }),
    );
    expect(notesOf(doc).read_page).toEqual(["Look closely", ""]);
  });

  test("is not offered on a loop or a read-only Doc", () => {
    renderLiveDoc([forLoop("each_row", null, [code("inner_step", null)])]);
    expect(addNoteButtons()).toHaveLength(1);
    cleanup();
    renderDoc([steps()[1]!]);
    expect(addNoteButtons()).toHaveLength(0);
  });

  test("on a step that keeps a note under it and has a step after it, it goes to that note", () => {
    const doc = renderLiveDoc([
      block("wait", "open_site", "read_page", {
        wait_sec: 5,
        trailing_notes: "Wait for the banner",
      }),
      steps()[1]!,
    ]);
    fireEvent.click(addNote("Open site"));
    const notes = screen.getAllByRole("textbox", { name: "Note" });
    expect(notes).toHaveLength(1);
    expect(document.activeElement).toBe(notes[0]);
    expect(notes[0]!.getAttribute("contenteditable")).toBe("plaintext-only");
    expect(notesOf(doc)).toEqual({
      open_site: ["", "Wait for the banner"],
      read_page: ["", ""],
    });
  });
});
