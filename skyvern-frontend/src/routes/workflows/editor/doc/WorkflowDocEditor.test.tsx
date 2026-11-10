// @vitest-environment jsdom
import { useRunValidationStore } from "../runValidation/useRunValidationStore";
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

import { ProxyLocation, Status } from "@/api/types";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";

import {
  WorkflowParameterValueType,
  type WorkflowBlock,
  type WorkflowSettings,
} from "../../types/workflowTypes";
import { isWorkflowBlockNode } from "../nodes";
import { getElements } from "../workflowEditorUtils";
import { useLocateBlockStore } from "../runValidation/useLocateBlockStore";
import { useDocRunStore } from "./docRunStore";
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

const docRun = vi.hoisted(() => ({ current: {} as Record<string, unknown> }));
vi.mock("./useDocRun", () => ({
  useDocRun: () => ({
    unknownLabels: new Set(),
    rowsLoaded: true,
    ...docRun.current,
  }),
}));
import type { DocGraph } from "./docGraphEdits";
import * as axiosClientModule from "@/api/AxiosClient";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";

vi.mock("../workflowYamlDocument", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../workflowYamlDocument")>()),
  buildWorkflowSaveRequest: () => ({}),
}));

// The run widgets need studio run contexts; this file tests navigation, so they are stubbed.
vi.mock("./DocStepResult", () => ({ DocStepResult: () => null }));
vi.mock("./DocRunStepButton", () => ({ DocRunStepButton: () => null }));
// The settings form is mocked because these tests check the side panel's header only.
vi.mock("../panels/BlockConfigForm", () => ({ BlockConfigForm: () => null }));

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
          <WorkflowDocEditor nodes={nodes} edges={edges} edits={edits} />
        </ReactFlowProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return nodes;
}

describe("WorkflowDocEditor navigation", () => {
  beforeEach(() => {
    docRun.current = { runId: null, blocksByLabel: new Map(), live: false };
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

  test("a run line above the steps says which run the results come from, and shows only with a run", () => {
    renderDoc();
    expect(screen.queryByTestId("doc-run-line")).toBeNull();
    cleanup();
    docRun.current = {
      runId: "wr_1",
      run: {
        status: Status.Failed,
        created_at: new Date().toISOString(),
        workflow: { version: 2 },
      },
      currentVersion: 3,
      earlierVersion: true,
      blocksByLabel: new Map(),
      live: false,
    };
    renderDoc();
    expect(screen.getByTestId("doc-run-line").textContent).toContain(
      "Run wr_1 · Failed · just now · an earlier version, v2",
    );
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

    test("a one-step run leaves the text editable but shows no insert line until it ends", () => {
      const steps = () => [
        block("goto_url", "open_site", "write_it", { url: "https://a.test" }),
        prompt(),
      ];
      docRun.current = {
        runId: "wr_1",
        blocksByLabel: new Map(),
        live: true,
        owned: true,
      };
      renderDoc(steps(), editsWithAdd(), "/?bl=open_site");
      expect(
        screen.queryByRole("button", { name: "Add a step here" }),
      ).toBeNull();
      fireEvent.focus(screen.getByRole("textbox", { name: "write_it prompt" }));
      expect(
        screen
          .getByRole("textbox", { name: "write_it prompt" })
          .getAttribute("contenteditable"),
      ).toBe("plaintext-only");
      cleanup();

      docRun.current = { ...docRun.current, live: false };
      renderDoc(steps(), editsWithAdd(), "/?bl=open_site");
      expect(
        screen.getByRole("button", { name: "Add a step here" }),
      ).toBeTruthy();
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

  test("Tab nests into the run branch displayed on screen without a manual selection", () => {
    docRun.current = {
      runId: "wr_1",
      blocksByLabel: new Map([
        [
          "route",
          { status: Status.Completed, executed_branch_id: "old_branch" },
        ],
        ["else_step", { status: Status.Completed }],
      ]),
      live: false,
    };
    const edits: DocGraphEdits = {
      apply: vi.fn(),
      update: vi.fn(),
      addStep: vi.fn(),
      duplicate: vi.fn(),
    };
    const nodes = renderDoc(
      [
        conditional("route", "after", [
          { id: "yes", next: "if_step", when: "Check the condition" },
          { id: "no", next: "else_step" },
        ]),
        code("if_step", "after"),
        code("else_step", "after"),
        block("text_prompt", "after", null, { prompt: "Do the next thing" }),
      ],
      edits,
    );
    expect(
      screen
        .getByRole("tab", { name: /Otherwise/ })
        .getAttribute("aria-selected"),
    ).toBe("true");
    fireEvent.focus(screen.getByRole("textbox", { name: "after prompt" }));
    fireEvent.keyDown(screen.getByRole("textbox", { name: "after prompt" }), {
      key: "Tab",
    });

    expect(edits.apply).toHaveBeenCalledOnce();
    const moved = vi
      .mocked(edits.apply)
      .mock.calls[0]![0].nodes.filter(isWorkflowBlockNode)
      .find((n) => n.data.label === "after")!;
    const route = nodes.find((n) => n.type === "conditional")!;
    expect(moved.parentId).toBe(route.id);
    expect(moved.data.conditionalBranchId).toBe(
      route.data.branches.find((b) => b.is_default)!.id,
    );
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

  test("a step offers its prompt's URL for its empty URL field after Step options, with the reason, and not while typing or read-only", () => {
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

    // Forward Tab follows the DOM: the prompt, then Step options, then the offer.
    const tabbable = [
      ...document.querySelectorAll<HTMLElement>('[role="textbox"], button'),
    ];
    const at = (el: HTMLElement) => tabbable.indexOf(el);
    const prompt = screen.getByRole("textbox", {
      name: "search_step navigationGoal",
    });
    const options = screen.getByRole("button", {
      name: /^Step options for .*search/i,
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

  test("counts server validation problems in the edit bar the same way Run counts them", async () => {
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

      // The same reference in two fields is one problem; the warning is listed but does not hold Run.
      expect(screen.getByText("2 problems, 1 warning")).toBeTruthy();
      expect(useRunValidationStore.getState().docProblemCount).toBe(2);
      expect(
        screen.getByText(/This block has an invalid setting\./),
      ).toBeTruthy();
    } finally {
      useWorkflowHasChangesStore.setState({ getSaveData });
      vi.useRealTimers();
      vi.restoreAllMocks();
      cleanup();
    }
  });

  test("a credential named in a note holds Run even though the server sends it as a warning", async () => {
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
      expect(useRunValidationStore.getState().docProblemCount).toBe(1);
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
      expect(useRunValidationStore.getState().docProblemCount).toBe(1);
      act(() => useWorkflowParametersStore.setState({ parameters: [] }));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000);
      });
      expect(screen.getByRole("alert").textContent).toContain(
        "Couldn't check this workflow: Workflow validation drafts must be at most 100000 bytes.",
      );
      expect(screen.getByText("2 warnings")).toBeTruthy();
      // The earlier check's warning describes an older draft, so it no longer holds the run.
      expect(useRunValidationStore.getState().docProblemCount).toBe(0);
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
      expect(screen.getByText("1 problem")).toBeTruthy();
    } finally {
      useWorkflowHasChangesStore.setState({ getSaveData });
      vi.useRealTimers();
      vi.restoreAllMocks();
      cleanup();
    }
  });

  test("a warning-only validation response shows the problem but doesn't hold Run", async () => {
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
      expect(useRunValidationStore.getState().docProblemCount).toBe(0);
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

  test("Step options names its step, lists the step's settings in place, and Edit in side panel opens the side panel", () => {
    renderDoc(
      [
        block("extraction", "find_prices", null, {
          data_extraction_goal: "Get the prices",
          max_retries: 3,
          max_steps_per_run: 12,
        }),
      ],
      { apply: vi.fn(), update: vi.fn(), addStep: vi.fn(), duplicate: vi.fn() },
    );
    expect(screen.getByText("Max retries: 3")).toBeTruthy();
    const toggle = screen.getByRole("button", {
      name: "Step options for Find prices",
    });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    const options = within(
      document.getElementById(toggle.getAttribute("aria-controls")!)!,
    );
    expect(options.getByText("Max steps").nextElementSibling?.textContent).toBe(
      "12",
    );
    // The extraction form has no max retries control, so the box leaves it out.
    expect(options.queryByText("Max retries")).toBeNull();
    expect(
      options.getByText("Continue on failure").nextElementSibling?.textContent,
    ).toBe("Off (default)");

    fireEvent.click(
      options.getByRole("button", { name: "Edit in side panel" }),
    );
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    const selected = within(screen.getByLabelText("Selected step"));
    expect(selected.getByText("Find prices")).toBeTruthy();
    expect(selected.getByText("find_prices")).toBeTruthy();
  });

  test("a step whose form edits none of the listed settings has no Step options", () => {
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
    expect(
      screen.queryAllByRole("button", { name: /^Step options for/ }),
    ).toEqual([]);
    expect(screen.queryByText(/Continue on failure|Model:/)).toBeNull();
  });

  test("Uses lists a step's declared inputs and its {{ }} reads, matching the input chips, and never a Code step's Python", () => {
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
    const uses = step("sign_in").getByText("Uses");
    expect(
      Array.from(uses.querySelectorAll("span"), (pill) => pill.textContent),
    ).toEqual(["site_login", "account_id"]);
    expect(step("count_rows").queryByText("Uses")).toBeNull();
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

  test("Step options lists the output fields of a jsonSchema step and of an extraction that returns a list of rows", () => {
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
        name: `Step options for ${title}`,
      });
      fireEvent.click(toggle);
      return within(
        document.getElementById(toggle.getAttribute("aria-controls")!)!,
      ).getByText("Output fields").nextElementSibling?.textContent;
    };
    expect(fields("Summarize page")).toBe("title, price");
    expect(fields("List orders")).toBe("order_id, total");
  });
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
    docRun.current = { runId: null, blocksByLabel: new Map(), live: false };
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
    expect(useRunValidationStore.getState().docProblemCount).toBe(0);

    fireEvent.pointerDown(document.body);

    expect(
      screen.getAllByText(
        "This step has no saved credential. Open Step options, then Edit in side panel to choose one.",
      ),
    ).toHaveLength(1);
    expect(document.querySelector("[data-doc-problems]")?.textContent).toBe(
      "1 problem",
    );
    expect(screen.queryByText(/need.* fixing/)).toBeNull();
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
    "a loop whose reference is %j with %s has no list and holds Run",
    (reference, _, loopOver) => {
      renderDoc([
        forLoop("each_result", null, [code("open_result", null)], {
          loop_over: loopOver,
          loop_variable_reference: reference,
        }),
      ]);
      expect(document.querySelector("[data-doc-step-fix]")).toBeTruthy();
      expect(useRunValidationStore.getState().docProblemCount).toBeGreaterThan(
        0,
      );
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

  test("a loop over a whole extraction result holds Run; the typed list path does not", () => {
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
    expect(useRunValidationStore.getState().docProblemCount).toBe(1);
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
    expect(useRunValidationStore.getState().docProblemCount).toBe(0);
  });

  test("the problems button lists every problem it counts, jumps to the step of the one picked, and a refused Run opens it in view", async () => {
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

    // A refused Run, from the Doc or the top bar, calls the callback the Doc registers.
    // It brings the list's button into view instead of jumping, so the list is never anchored off screen.
    scroll.mockClear();
    act(() => {
      useRunValidationStore.getState().docShowProblems?.();
    });
    expect(
      await screen.findByRole("dialog", { name: "3 problems" }),
    ).toBeTruthy();
    expect(scroll.mock.contexts).toEqual([trigger]);
    fireEvent.keyDown(document.activeElement ?? document.body, {
      key: "Escape",
    });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() =>
      expect(
        document.activeElement
          ?.closest("[data-doc-node-id]")
          ?.getAttribute("data-doc-node-id"),
      ).toBe(site.id),
    );
  });

  test("Escape on a list a refused Run opened while typing returns focus to the line, not the page", async () => {
    const nodes = renderDoc(
      [
        block("goto_url", "open_site", "write_it", { url: "" }),
        block("text_prompt", "write_it", null, { prompt: "Do the next thing" }),
      ],
      { apply: vi.fn(), update: vi.fn(), addStep: vi.fn(), duplicate: vi.fn() },
    );
    const site = nodes.find((node) => node.data.label === "write_it")!;
    const step = document.querySelector<HTMLElement>(
      `[data-doc-node-id="${site.id}"]`,
    )!;
    act(() => screen.getByRole("textbox", { name: "write_it prompt" }).focus());
    const typing = document.activeElement as HTMLElement;
    expect(typing.closest("[data-doc-node-id]")).toBe(step);
    expect(typing.getAttribute("contenteditable")).toBe("plaintext-only");

    act(() => {
      useRunValidationStore.getState().docShowProblems?.();
    });
    await screen.findByRole("dialog", { name: "1 problem" });
    await waitFor(() => expect(typing.isConnected).toBe(false));
    fireEvent.keyDown(document.activeElement ?? document.body, {
      key: "Escape",
    });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() =>
      expect(document.activeElement?.closest("[data-doc-node-id]")).toBe(
        document.querySelector(`[data-doc-node-id="${site.id}"]`),
      ),
    );
  });

  test("a freshly added loop over a picked whole result holds Run once the user leaves it", () => {
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
    expect(useRunValidationStore.getState().docProblemCount).toBe(0);

    fireEvent.pointerDown(document.body);

    expect(
      screen.getByText(
        /^extraction_1 has no top-level list in its Data Schema/,
      ),
    ).toBeTruthy();
    expect(useRunValidationStore.getState().docProblemCount).toBe(1);
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
    docRun.current = { runId: null, blocksByLabel: new Map(), live: false };
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
    docRun.current = { runId: null, blocksByLabel: new Map(), live: false };
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

  test("a credential input no step uses, with no saved credential, is not needed to run", () => {
    useWorkflowParametersStore.setState({
      parameters: [
        {
          key: "site_login",
          parameterType: "credential",
          dataType: WorkflowParameterValueType.CredentialId,
          credentialId: "",
          description: null,
        } as never,
      ],
    });
    renderDoc([code("tidy_up", null)]);
    expect(
      useDocRunStore.getState().inputs.map((input) => input.key),
    ).not.toContain("site_login");
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
    docRun.current = { runId: null, blocksByLabel: new Map(), live: false };
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
