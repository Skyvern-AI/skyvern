// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { ReactFlowProvider, useNodes, useReactFlow } from "@xyflow/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { getClient } from "@/api/AxiosClient";

import { flushBufferedEditorEdits } from "@/hooks/useDeferredLockedEdit";

import { useCopilotActionStore } from "@/store/useCopilotActionStore";

import type { AppNode } from "./nodes";
import {
  codeBlockNodeDefaultData,
  type CodeBlockNodeData,
} from "./nodes/CodeBlockNode/types";
import { PendingGoalChangesDialog } from "./PendingGoalChangesDialog";
import { PendingGoalChangesPublisher } from "./PendingGoalChangesPublisher";
import { WorkflowScopeContext } from "./WorkflowScopeContext";

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

const post = vi.fn();
vi.mock("@/api/AxiosClient", () => ({
  getClient: vi.fn(async () => ({ post })),
}));

afterEach(cleanup);

const editedBlock = {
  id: "cb1",
  type: "codeBlock",
  position: { x: 0, y: 0 },
  data: {
    ...codeBlockNodeDefaultData,
    label: "find_provider",
    prompt: "Return every provider's phone number",
    userOwnedGoal: true,
    goalNeedsRegeneration: true,
    goalBeforeEdit: {
      prompt: "Return the first provider's address",
      userOwnedGoal: null,
      goalNeedsRegeneration: null,
    },
  },
} as AppNode;

test("publishes a pending Goal change and undoes it in the canvas", () => {
  render(
    <ReactFlowProvider defaultNodes={[editedBlock]}>
      <PendingGoalChangesPublisher />
    </ReactFlowProvider>,
  );

  expect(useCopilotActionStore.getState().pendingGoalChanges).toEqual([
    {
      label: "find_provider",
      goal: "Return every provider's phone number",
      previousGoal: "Return the first provider's address",
    },
  ]);

  act(() => {
    useCopilotActionStore.getState().undoGoalChange("find_provider");
  });

  expect(useCopilotActionStore.getState().pendingGoalChanges).toEqual([]);
});

test("undo and save saves after the canvas has dropped the new Goal", () => {
  const consoleError = vi.spyOn(console, "error");
  const onOpenChange = vi.fn();
  let changesAtSave: number | undefined;
  const save = vi.fn(() => {
    changesAtSave = useCopilotActionStore.getState().pendingGoalChanges.length;
    // The real save forces buffered edits through with flushSync before it writes.
    flushBufferedEditorEdits();
  });
  render(
    <ReactFlowProvider defaultNodes={[editedBlock]}>
      <PendingGoalChangesPublisher />
      <PendingGoalChangesDialog
        open
        onOpenChange={onOpenChange}
        onSave={save}
      />
    </ReactFlowProvider>,
  );

  fireEvent.click(
    screen.getByRole("button", { name: "Undo Goal change and save" }),
  );

  expect(save).toHaveBeenCalledOnce();
  expect(changesAtSave).toBe(0);
  expect(onOpenChange).toHaveBeenCalledWith(false);
  expect(consoleError).not.toHaveBeenCalledWith(
    expect.stringContaining("flushSync was called from inside a lifecycle"),
    expect.anything(),
  );
  consoleError.mockRestore();
});

describe("a Goal update offered after a hand code edit", () => {
  const editedGoal = "Read the order total";
  const handEditedCode = "return {'total': 1, 'currency': 'USD'}";
  const handEdited = {
    id: "cb1",
    type: "codeBlock",
    position: { x: 0, y: 0 },
    data: {
      ...codeBlockNodeDefaultData,
      label: "read_total",
      code: handEditedCode,
      prompt: editedGoal,
      userOwnedGoal: true,
      goalNeedsRegeneration: false,
      codeEditedByHand: true,
    },
  } as AppNode;
  const suggested = "The order page shows its total and currency.";
  let liveData: () => CodeBlockNodeData;
  let editGoal: (prompt: string) => void;

  function Probe() {
    const nodes = useNodes<AppNode>();
    const { updateNodeData } = useReactFlow<AppNode>();
    liveData = () => nodes[0]!.data as CodeBlockNodeData;
    editGoal = (prompt) => updateNodeData("cb1", { prompt });
    return null;
  }

  function renderBlock(
    data: Partial<CodeBlockNodeData> = {},
    readOnly = false,
  ) {
    render(
      <WorkflowScopeContext.Provider value={{ workflowId: null, readOnly }}>
        <ReactFlowProvider
          defaultNodes={[
            { ...handEdited, data: { ...handEdited.data, ...data } } as AppNode,
          ]}
        >
          <PendingGoalChangesPublisher />
          <Probe />
        </ReactFlowProvider>
      </WorkflowScopeContext.Provider>,
    );
  }

  async function requestSuggestion() {
    await act(async () => {
      useCopilotActionStore.getState().updateGoal("read_total");
    });
  }

  beforeEach(() => {
    useCopilotActionStore.setState(useCopilotActionStore.getInitialState());
    post.mockReset();
    post.mockResolvedValue({ data: { goal: suggested } });
  });

  test("Keep Goal clears the notice and leaves the Goal byte-for-byte as it was", () => {
    renderBlock();

    act(() => {
      useCopilotActionStore.getState().keepGoal("read_total");
    });

    expect(liveData().prompt).toBe(editedGoal);
    expect(liveData().userOwnedGoal).toBe(true);
    expect(liveData().codeEditedByHand).toBe(false);
    expect(useCopilotActionStore.getState().codeEditedBlocks).toEqual([]);
  });

  test.each([
    ["applies", {}, false],
    [
      "changes nothing while the block is generating",
      { generatingBlockLabel: "read_total" },
      true,
    ],
  ])(
    "Keep my code on a Goal typed after a hand edit %s",
    (_, state, refused) => {
      useCopilotActionStore.setState(state);
      renderBlock({
        goalNeedsRegeneration: true,
        goalBeforeEdit: {
          prompt: "Read the total",
          userOwnedGoal: false,
          goalNeedsRegeneration: false,
        },
      });

      act(() => {
        useCopilotActionStore.getState().keepCode("read_total");
      });

      expect(liveData().prompt).toBe(editedGoal);
      expect(liveData().userOwnedGoal).toBe(true);
      expect(liveData().goalNeedsRegeneration).toBe(refused);
      expect(liveData().codeEditedByHand).toBe(refused);
      expect(useCopilotActionStore.getState().pendingGoalChanges.length).toBe(
        refused ? 1 : 0,
      );
    },
  );

  test("Update Goal sends the code without the data schema, and Accept makes the suggestion the person's Goal", async () => {
    renderBlock({ dataSchema: '{"type": "object"}' });

    expect(useCopilotActionStore.getState().codeEditedBlocks).toEqual([
      { label: "read_total", goal: editedGoal, suggestedGoal: null },
    ]);
    await requestSuggestion();

    expect(vi.mocked(getClient)).toHaveBeenCalled();
    expect(post).toHaveBeenCalledWith("/workflow/copilot/suggest-goal", {
      label: "read_total",
      code: handEditedCode,
      current_goal: editedGoal,
      parameter_keys: [],
    });
    expect(liveData().prompt).toBe(editedGoal);
    expect(useCopilotActionStore.getState().codeEditedBlocks).toEqual([
      { label: "read_total", goal: editedGoal, suggestedGoal: suggested },
    ]);

    act(() => {
      useCopilotActionStore.getState().acceptGoal("read_total");
    });

    expect(liveData()).toMatchObject({
      prompt: suggested,
      userOwnedGoal: true,
      goalNeedsRegeneration: false,
      codeEditedByHand: false,
    });
    expect(useCopilotActionStore.getState().pendingGoalChanges).toEqual([]);
  });

  test("a suggestion requested before the person edited the Goal cannot be accepted", async () => {
    let resolve: (value: { data: { goal: string } }) => void = () => {};
    post.mockReturnValue(new Promise((settle) => (resolve = settle)));
    renderBlock();

    act(() => {
      useCopilotActionStore.getState().updateGoal("read_total");
    });
    act(() => editGoal("Read the order total and currency"));
    await act(async () => resolve({ data: { goal: suggested } }));
    act(() => {
      useCopilotActionStore.getState().acceptGoal("read_total");
    });

    expect(liveData().prompt).toBe("Read the order total and currency");
  });

  test("Keep Goal while Update Goal is in flight leaves no suggestion behind", async () => {
    let resolve: (value: { data: { goal: string } }) => void = () => {};
    post.mockReturnValue(new Promise((settle) => (resolve = settle)));
    renderBlock();

    act(() => {
      useCopilotActionStore.getState().updateGoal("read_total");
    });
    act(() => {
      useCopilotActionStore.getState().keepGoal("read_total");
    });
    await act(async () => resolve({ data: { goal: suggested } }));

    expect(useCopilotActionStore.getState().goalSuggestions).toEqual({});
    expect(useCopilotActionStore.getState().suggestingGoalLabels).toEqual([]);
  });

  test.each([
    ["not editable", {}, { editable: false }, false],
    ["in a read-only scope", {}, {}, true],
    ["generating", { generatingBlockLabel: "read_total" }, {}, false],
    [
      "queued",
      {
        generatingBlockLabel: "other",
        queuedBuilds: [{ blockLabel: "read_total", prompt: editedGoal }],
      },
      {},
      false,
    ],
  ])(
    "Update Goal, Accept and Keep Goal change nothing while the block is %s",
    async (_, state, data, readOnly) => {
      useCopilotActionStore.setState({
        ...state,
        goalSuggestions: {
          read_total: {
            forCode: handEditedCode,
            forGoal: editedGoal,
            goal: suggested,
          },
        },
      });
      renderBlock(data, readOnly);

      await requestSuggestion();
      act(() => {
        useCopilotActionStore.getState().acceptGoal("read_total");
        useCopilotActionStore.getState().keepGoal("read_total");
      });

      expect(post).not.toHaveBeenCalled();
      expect(liveData().prompt).toBe(editedGoal);
      expect(liveData().codeEditedByHand).toBe(true);
    },
  );
});
