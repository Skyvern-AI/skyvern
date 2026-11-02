// @vitest-environment jsdom
import { useEffect, useState } from "react";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ReactFlowProvider } from "@xyflow/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useBlockPlayRegistry } from "@/store/BlockPlayRegistryStore";
import { useWorkflowYamlEditorStore } from "@/store/WorkflowYamlEditorStore";
import type { WorkflowRunBlock } from "../../types/workflowRunTypes";
import { WorkflowScopeContext } from "../WorkflowScopeContext";
import { getElements } from "../workflowEditorUtils";
import { block, conditional, settings } from "./docTestBlocks";
import { WorkflowDocEditor } from "./WorkflowDocEditor";

const control = vi.hoisted(() => ({
  save: Promise.resolve(),
  submit: Promise.resolve(),
  starts: vi.fn(),
  submits: vi.fn(),
  mounts: vi.fn(),
  unmounts: vi.fn(),
  blocksByLabel: new Map<string, WorkflowRunBlock>(),
}));
vi.mock("./useDocRun", () => ({
  useDocRun: () => ({
    runId: null,
    blocksByLabel: control.blocksByLabel,
    live: false,
  }),
}));
vi.mock("./DocStepResult", () => ({ DocStepResult: () => null }));
vi.mock("./DocLiveIndicator", () => ({ DocLiveIndicator: () => null }));
vi.mock("../nodes/components/NodeHeader", () => ({
  NodeHeader: ({ nodeId }: { nodeId: string }) => {
    const [pending, setPending] = useState(false);
    const [parameters, setParameters] = useState("");
    useEffect(() => {
      control.mounts(nodeId);
      return () => {
        control.unmounts(nodeId);
      };
    }, [nodeId]);
    useEffect(() => {
      useBlockPlayRegistry.getState().register(nodeId, {
        source: "doc",
        inert: pending,
        playing: false,
        stopping: false,
        stop: () => {},
        play: async () => {
          control.starts();
          setPending(true);
          useWorkflowYamlEditorStore.setState({ commitInProgress: true });
          try {
            await control.save;
            useWorkflowYamlEditorStore.setState({ commitInProgress: false });
            control.submits();
            await control.submit;
          } catch {
            /* The host keeps its parameter dialog on a refused save. */
          } finally {
            useWorkflowYamlEditorStore.setState({ commitInProgress: false });
            setPending(false);
          }
        },
      });
      return () => useBlockPlayRegistry.getState().unregister(nodeId, "doc");
    }, [nodeId, pending]);
    return (
      <input
        aria-label="Run parameters"
        value={parameters}
        onChange={(event) => setParameters(event.target.value)}
      />
    );
  },
}));

beforeEach(() => {
  control.starts.mockClear();
  control.submits.mockClear();
  control.mounts.mockClear();
  control.unmounts.mockClear();
  useBlockPlayRegistry.setState({ handles: {} });
  useWorkflowYamlEditorStore.setState(
    useWorkflowYamlEditorStore.getInitialState(),
  );
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it.each([
  ["navigation", false],
  ["conditional", false],
  ["navigation", true],
  ["conditional", true],
] as const)(
  "keeps the %s host and parameters across the save lock (save fails: %s)",
  async (type, failed) => {
    let finishSave!: () => void;
    let failSave!: (error: Error) => void;
    let finishSubmit!: () => void;
    control.save = new Promise<void>((resolve, reject) => {
      finishSave = resolve;
      failSave = reject;
    });
    control.submit = new Promise<void>((resolve) => {
      finishSubmit = resolve;
    });
    const step =
      type === "conditional"
        ? conditional("step", null, [
            { id: "yes", next: null, when: "Check" },
            { id: "no", next: null },
          ])
        : block("navigation", "step", null, {
            navigation_goal: "Open the page",
            parameters: [],
          });
    const { nodes, edges } = getElements([step], settings(), true);
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <ReactFlowProvider>
            <WorkflowDocEditor nodes={nodes} edges={edges} />
          </ReactFlowProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const run = await screen.findByRole("button", { name: "Run step" });
    const mounts = control.mounts.mock.calls.length;
    const unmounts = control.unmounts.mock.calls.length;
    fireEvent.change(screen.getByRole("textbox", { name: "Run parameters" }), {
      target: { value: "retained input" },
    });
    fireEvent.click(run);
    expect(
      (screen.getByRole("button", { name: "Run step" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect(control.mounts).toHaveBeenCalledTimes(mounts);
    expect(control.unmounts).toHaveBeenCalledTimes(unmounts);
    await act(async () => {
      if (failed) failSave(new Error("save refused"));
      else finishSave();
    });
    expect(
      (
        screen.getByRole("textbox", {
          name: "Run parameters",
        }) as HTMLInputElement
      ).value,
    ).toBe("retained input");
    if (!failed) {
      const stillPending = screen.getByRole("button", {
        name: "Run step",
      }) as HTMLButtonElement;
      expect(stillPending.disabled).toBe(true);
      fireEvent.click(stillPending);
      expect(control.starts).toHaveBeenCalledOnce();
      expect(control.submits).toHaveBeenCalledOnce();
      await act(async () => finishSubmit());
    } else expect(control.submits).not.toHaveBeenCalled();
    expect(
      (screen.getByRole("button", { name: "Run step" }) as HTMLButtonElement)
        .disabled,
    ).toBe(false);
  },
);

it("does not mount a run host for a read-only workflow scope", () => {
  const step = block("navigation", "step", null, {
    navigation_goal: "Open the page",
    parameters: [],
  });
  const { nodes, edges } = getElements([step], settings(), true);
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <ReactFlowProvider>
          <WorkflowScopeContext.Provider
            value={{ workflowId: "wpid_read_only", readOnly: true }}
          >
            <WorkflowDocEditor nodes={nodes} edges={edges} />
          </WorkflowScopeContext.Provider>
        </ReactFlowProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  expect(screen.queryByRole("button", { name: "Run step" })).toBeNull();
  expect(control.mounts).not.toHaveBeenCalled();
});
