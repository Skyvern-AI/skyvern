import { EditorView } from "@codemirror/view";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { ReactFlowProvider } from "@xyflow/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { clearDeferredEdits } from "@/hooks/useDeferredLockedEdit";
import { useWorkflowYamlEditorStore } from "@/store/WorkflowYamlEditorStore";

import { WorkflowScopeContext } from "../../WorkflowScopeContext";
import { WebSearchEditor } from "./WebSearchEditor";
import {
  webSearchNodeDefaultData,
  type WebSearchNode,
  type WebSearchNodeData,
} from "./types";

const update = vi.fn();

vi.mock("../../useUpdate", () => ({
  useUpdate: () => update,
}));

vi.mock("../TaskNode/ParametersMultiSelect", () => ({
  ParametersMultiSelect: () => null,
}));

vi.mock("@/components/ModelSelector", () => ({
  ModelSelector: () => null,
}));

afterEach(() => {
  cleanup();
  clearDeferredEdits();
  useWorkflowYamlEditorStore.setState({ copilotAcceptance: null });
  update.mockClear();
});

function renderEditor(data: Partial<WebSearchNodeData> = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const node: WebSearchNode = {
    id: "web-search-1",
    type: "web_search",
    position: { x: 0, y: 0 },
    data: { ...webSearchNodeDefaultData, query: "original query", ...data },
  };
  return render(
    <QueryClientProvider client={client}>
      <ReactFlowProvider defaultNodes={[node]}>
        <WorkflowScopeContext.Provider
          value={{ workflowId: "wpid_web_search", readOnly: false }}
        >
          <WebSearchEditor blockId={node.id} />
        </WorkflowScopeContext.Provider>
      </ReactFlowProvider>
    </QueryClientProvider>,
  );
}

describe("WebSearchEditor deferred edits", () => {
  test("replays the buffered JSON schema once after a locked unmount and remount", () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    vi.stubGlobal("IntersectionObserver", undefined);
    try {
      const data = { prompt: "Summarize the results", jsonSchema: "{}" };
      const first = renderEditor(data);
      const editor = EditorView.findFromDOM(
        first.container.querySelector<HTMLElement>(".cm-content")!,
      )!;
      const pendingValue =
        '{"type":"object","properties":{"summary":{"type":"string"}}}';
      act(() =>
        editor.dispatch({
          changes: {
            from: 0,
            to: editor.state.doc.length,
            insert: pendingValue,
          },
        }),
      );
      act(() => vi.advanceTimersByTime(100));
      expect(update).not.toHaveBeenCalled();

      act(() => {
        useWorkflowYamlEditorStore.setState({
          copilotAcceptance: Symbol("recovery"),
        });
        first.unmount();
      });
      const second = renderEditor(data);
      act(() => vi.advanceTimersByTime(300));
      expect(update).not.toHaveBeenCalled();

      act(() =>
        useWorkflowYamlEditorStore.setState({ copilotAcceptance: null }),
      );
      expect(update).toHaveBeenCalledExactlyOnceWith({
        jsonSchema: pendingValue,
      });
      const restoredEditor = EditorView.findFromDOM(
        second.container.querySelector<HTMLElement>(".cm-content")!,
      )!;
      expect(restoredEditor.state.doc.toString()).toBe(pendingValue);
      act(() => vi.advanceTimersByTime(300));
      expect(update).toHaveBeenCalledTimes(1);
    } finally {
      cleanup();
      vi.useRealTimers();
      vi.unstubAllGlobals();
    }
  });

  test.each([
    { field: "query", placeholder: "site:example.com search terms" },
    {
      field: "prompt",
      placeholder: "What would you like to do with the search results?",
    },
  ])(
    "replays the buffered $field once after a locked unmount and remount",
    ({ field, placeholder }) => {
      vi.useFakeTimers();
      try {
        const first = renderEditor();
        expect(screen.getByTestId("web-search-block-form")).toBeTruthy();
        const pendingValue = `pending ${field}`;
        fireEvent.change(screen.getByPlaceholderText(placeholder), {
          target: { value: pendingValue },
        });
        act(() => vi.advanceTimersByTime(100));
        expect(update).not.toHaveBeenCalled();

        act(() => {
          useWorkflowYamlEditorStore.setState({
            copilotAcceptance: Symbol("recovery"),
          });
          first.unmount();
        });
        renderEditor();
        act(() => vi.advanceTimersByTime(300));
        expect(update).not.toHaveBeenCalled();

        act(() =>
          useWorkflowYamlEditorStore.setState({ copilotAcceptance: null }),
        );
        expect(update).toHaveBeenCalledExactlyOnceWith({
          [field]: pendingValue,
        });
        expect(
          (screen.getByPlaceholderText(placeholder) as HTMLTextAreaElement)
            .value,
        ).toBe(pendingValue);
        act(() => vi.advanceTimersByTime(300));
        expect(update).toHaveBeenCalledTimes(1);
      } finally {
        vi.useRealTimers();
      }
    },
  );
});
