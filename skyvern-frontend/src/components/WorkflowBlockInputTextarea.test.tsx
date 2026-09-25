// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react";
import axios from "axios";
import { ComponentProps } from "react";
import * as AxiosClient from "@/api/AxiosClient";
import { ReactFlowProvider } from "@xyflow/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import {
  clearDeferredEdits,
  useDeferredLockedEdit,
} from "@/hooks/useDeferredLockedEdit";
import { useWorkflowYamlEditorStore } from "@/store/WorkflowYamlEditorStore";

import { WorkflowScopeContext } from "@/routes/workflows/editor/WorkflowScopeContext";

import { WorkflowBlockInputTextarea } from "./WorkflowBlockInputTextarea";

afterEach(() => {
  cleanup();
  clearDeferredEdits();
  useWorkflowYamlEditorStore.setState({
    commitInProgress: false,
    copilotAcceptance: null,
  });
  vi.restoreAllMocks();
  vi.useRealTimers();
});

function renderTextarea(
  readOnly: boolean,
  props: Partial<ComponentProps<typeof WorkflowBlockInputTextarea>> = {},
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ReactFlowProvider>
        <WorkflowScopeContext.Provider value={{ workflowId: "w", readOnly }}>
          <WorkflowBlockInputTextarea
            nodeId="n1"
            value="goal text"
            aiImprove={{ useCase: "navigation" }}
            onChange={() => {}}
            {...props}
          />
        </WorkflowScopeContext.Provider>
      </ReactFlowProvider>
    </QueryClientProvider>,
  );
}

describe("WorkflowBlockInputTextarea in a read-only scope", () => {
  test("stays editable with actions in the live editor scope", () => {
    renderTextarea(false);
    const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
    expect(textarea.readOnly).toBe(false);
    expect(screen.queryByTestId("block-textarea-actions")).not.toBeNull();
  });

  // Comparison canvases need the prompt readable (still in the a11y tree) but not editable, and no prompt-improve action.
  test("is readOnly and hides actions in a read-only comparison scope", () => {
    renderTextarea(true);
    const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
    expect(textarea.readOnly).toBe(true);
    expect(screen.queryByTestId("block-textarea-actions")).toBeNull();
  });
});

describe("WorkflowBlockInputTextarea action visibility", () => {
  test.each([{ disabled: true }, { hideActions: true }])(
    "hides actions for %j even after a lock cycle",
    (props) => {
      renderTextarea(false, props);
      expect(screen.queryByTestId("block-textarea-actions")).toBeNull();
      act(() =>
        useWorkflowYamlEditorStore.setState({ commitInProgress: true }),
      );
      act(() =>
        useWorkflowYamlEditorStore.setState({ commitInProgress: false }),
      );
      expect(screen.queryByTestId("block-textarea-actions")).toBeNull();
    },
  );

  test("omits Improve Prompt when the field has no aiImprove configuration", () => {
    renderTextarea(false, { aiImprove: undefined });
    expect(
      screen.getByTestId("block-textarea-actions").querySelectorAll("svg"),
    ).toHaveLength(1);
  });
});

describe.each(["save", "copilot"] as const)(
  "Improve Prompt during a %s lock",
  (lock) => {
    test.each(["pending", "open"] as const)(
      "preserves the %s suggestion and disables changes until unlock",
      async (phase) => {
        const onChange = vi.fn();
        const client = axios.create();
        vi.spyOn(AxiosClient, "getClient").mockResolvedValue(client);
        let resolveSuggestion!: (value: unknown) => void;
        const post = vi.spyOn(client, "post").mockImplementation(
          () =>
            new Promise((resolve) => {
              resolveSuggestion = resolve;
            }),
        );
        renderTextarea(false, { onChange });
        const actions = screen.getByTestId("block-textarea-actions");
        fireEvent.click(actions.querySelector("svg")!);
        await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
        const complete = async () => {
          await act(async () =>
            resolveSuggestion({
              data: {
                original: "goal text",
                improved: "More precise goal",
                error: null,
              },
            }),
          );
          await screen.findByRole("dialog", { name: "Choose Your Prompt" });
        };
        if (phase === "open") {
          await complete();
          fireEvent.click(screen.getByText("Original"));
        }
        act(() =>
          useWorkflowYamlEditorStore.setState(
            lock === "save"
              ? { commitInProgress: true }
              : { copilotAcceptance: Symbol("turn") },
          ),
        );
        expect(screen.getByTestId("block-textarea-actions")).toBe(actions);
        if (phase === "pending") await complete();
        const expectedPrompt =
          phase === "open" ? "goal text" : "More precise goal";
        expect(
          screen.getByText(expectedPrompt, { selector: "p" }),
        ).not.toBeNull();
        const accept = screen.getByRole("button", {
          name: "Use This Prompt",
        }) as HTMLButtonElement;
        const trigger = actions.querySelector("button")!;
        expect(trigger.disabled).toBe(true);
        expect(accept.disabled).toBe(true);
        fireEvent.click(trigger);
        fireEvent.click(accept);
        expect(post).toHaveBeenCalledTimes(1);
        expect(onChange).not.toHaveBeenCalled();
        expect(screen.getByRole("dialog")).not.toBeNull();
        act(() =>
          useWorkflowYamlEditorStore.setState({
            commitInProgress: false,
            copilotAcceptance: null,
          }),
        );
        expect(trigger.disabled).toBe(false);
        expect(accept.disabled).toBe(false);
        expect(
          screen.getByText(expectedPrompt, { selector: "p" }),
        ).not.toBeNull();
        fireEvent.click(accept);
        await waitFor(() =>
          expect(onChange).toHaveBeenCalledExactlyOnceWith(expectedPrompt),
        );
        expect(screen.queryByRole("dialog")).toBeNull();
      },
    );
  },
);

describe.each([undefined, "node-draft:prompt"])(
  "useDeferredLockedEdit with deferKey %s",
  (deferKey) => {
    test("emits the buffered edit when Reject restores the original prop before unlock", () => {
      vi.useFakeTimers();
      const onChange = vi.fn();
      const { result, rerender } = renderHook(
        ({ value }) => useDeferredLockedEdit({ value, onChange, deferKey }),
        { initialProps: { value: "original" } },
      );
      act(() => result.current.onChange("buffered"));
      act(() =>
        useWorkflowYamlEditorStore.setState({
          copilotAcceptance: Symbol("turn"),
        }),
      );
      rerender({ value: "draft" });
      act(() => vi.advanceTimersByTime(300));
      expect(onChange).not.toHaveBeenCalled();

      rerender({ value: "original" });
      expect(onChange).not.toHaveBeenCalled();
      act(() =>
        useWorkflowYamlEditorStore.setState({ copilotAcceptance: null }),
      );

      expect(result.current.value).toBe("buffered");
      expect(onChange).toHaveBeenCalledExactlyOnceWith("buffered");
      act(() => vi.advanceTimersByTime(300));
      expect(onChange).toHaveBeenCalledTimes(1);
    });

    test("drops the buffered edit when Accept keeps the changed prop", () => {
      vi.useFakeTimers();
      const onChange = vi.fn();
      const { result, rerender } = renderHook(
        ({ value }) => useDeferredLockedEdit({ value, onChange, deferKey }),
        { initialProps: { value: "original" } },
      );
      act(() => result.current.onChange("buffered"));
      act(() =>
        useWorkflowYamlEditorStore.setState({
          copilotAcceptance: Symbol("turn"),
        }),
      );
      rerender({ value: "draft" });
      act(() => vi.advanceTimersByTime(300));
      expect(onChange).not.toHaveBeenCalled();
      act(() =>
        useWorkflowYamlEditorStore.setState({ copilotAcceptance: null }),
      );

      expect(result.current.value).toBe("draft");
      act(() => vi.advanceTimersByTime(300));
      expect(onChange).not.toHaveBeenCalled();
    });
  },
);

describe("useDeferredLockedEdit across unmounts", () => {
  test("applies a keyed buffer once after remount and lock release", () => {
    vi.useFakeTimers();
    const onChange = vi.fn();
    const props = {
      value: "original",
      onChange,
      deferKey: "node-restore:prompt",
    };
    const first = renderHook(() => useDeferredLockedEdit(props));
    act(() => first.result.current.onChange("buffered"));
    act(() => {
      useWorkflowYamlEditorStore.setState({ commitInProgress: true });
      first.unmount();
    });
    const second = renderHook(() => useDeferredLockedEdit(props));
    act(() => vi.advanceTimersByTime(300));
    expect(onChange).not.toHaveBeenCalled();
    act(() => useWorkflowYamlEditorStore.setState({ commitInProgress: false }));
    expect(second.result.current.value).toBe("buffered");
    expect(onChange).toHaveBeenCalledExactlyOnceWith("buffered");
    second.unmount();
    renderHook(() => useDeferredLockedEdit(props));
    act(() => vi.advanceTimersByTime(300));
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  test("drops a keyed buffer when the prop changes between mounts", () => {
    vi.useFakeTimers();
    const onChange = vi.fn();
    const props = {
      value: "original",
      onChange,
      deferKey: "node-replaced:prompt",
    };
    const first = renderHook(() => useDeferredLockedEdit(props));
    act(() => first.result.current.onChange("buffered"));
    act(() => useWorkflowYamlEditorStore.setState({ commitInProgress: true }));
    first.unmount();
    const second = renderHook(() =>
      useDeferredLockedEdit({ ...props, value: "replacement" }),
    );
    act(() => useWorkflowYamlEditorStore.setState({ commitInProgress: false }));
    expect(second.result.current.value).toBe("replacement");
    expect(onChange).not.toHaveBeenCalled();
    second.unmount();
    renderHook(() => useDeferredLockedEdit(props));
    act(() => vi.advanceTimersByTime(300));
    expect(onChange).not.toHaveBeenCalled();
  });
});
