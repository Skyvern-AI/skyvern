// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { stringify as toYaml } from "yaml";

import type { BlockYAML } from "@/routes/workflows/types/workflowYamlTypes";
import { isDraftDirty } from "@/routes/workflows/editor/workflowChangesSummary";
import {
  subscribeToYamlDraftChanges,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";

import {
  useWorkflowHasChangesStore,
  type WorkflowSaveData,
} from "./WorkflowHasChangesStore";
import { useWorkflowSnapshotStore } from "./WorkflowSnapshotStore";

const block = (label: string, extra: Record<string, unknown> = {}): BlockYAML =>
  ({ label, block_type: "task", ...extra }) as unknown as BlockYAML;

const saveData = (blocks: Array<BlockYAML>): WorkflowSaveData =>
  ({
    title: "T",
    blocks,
    parameters: [],
    settings: { proxyLocation: "RESIDENTIAL" },
    workflow: {
      title: "T",
      workflow_definition: { version: 2, blocks: [], parameters: [] },
    },
  }) as unknown as WorkflowSaveData;

/** Point getSaveData at a fresh draft — the store reads it live on each call. */
function setDraft(blocks: Array<BlockYAML>) {
  useWorkflowHasChangesStore.setState({ getSaveData: () => saveData(blocks) });
}

beforeEach(() => {
  useWorkflowSnapshotStore.getState().clearSnapshot();
});

afterEach(() => {
  useWorkflowSnapshotStore.getState().clearSnapshot();
  useWorkflowHasChangesStore.setState({ getSaveData: () => null });
  useWorkflowYamlEditorStore.setState({
    active: false,
    draft: "",
    entrySnapshot: "",
  });
});

describe("WorkflowSnapshotStore lifecycle", () => {
  it("captureSnapshot freezes the current draft and starts clean", () => {
    setDraft([block("a", { url: "x" })]);
    useWorkflowSnapshotStore.getState().captureSnapshot();
    const state = useWorkflowSnapshotStore.getState();
    expect(state.snapshot).not.toBeNull();
    expect(state.contentDirty).toBe(false);
    expect(state.userHasEdited).toBe(false);
  });

  it("clearSnapshot drops the baseline and resets the flags", () => {
    setDraft([block("a")]);
    useWorkflowSnapshotStore.getState().captureSnapshot();
    useWorkflowSnapshotStore.getState().noteDraftChange(true);
    useWorkflowSnapshotStore.getState().clearSnapshot();
    const state = useWorkflowSnapshotStore.getState();
    expect(state.snapshot).toBeNull();
    expect(state.contentDirty).toBe(false);
    expect(state.userHasEdited).toBe(false);
  });

  it("is a no-op with no baseline yet (nothing to diff against)", () => {
    setDraft([block("a")]);
    useWorkflowSnapshotStore.getState().noteDraftChange(true);
    expect(useWorkflowSnapshotStore.getState().contentDirty).toBe(false);
    expect(useWorkflowSnapshotStore.getState().snapshot).toBeNull();
  });
});

describe("WorkflowSnapshotStore dirty-refresh (user edits)", () => {
  it("flags a canvas edit against the baseline", () => {
    setDraft([block("a", { url: "x" })]);
    useWorkflowSnapshotStore.getState().captureSnapshot();

    setDraft([block("a", { url: "y" })]);
    useWorkflowSnapshotStore.getState().noteDraftChange(true);

    const state = useWorkflowSnapshotStore.getState();
    expect(state.contentDirty).toBe(true);
    expect(state.userHasEdited).toBe(true);
  });

  it("flags a YAML-draft edit against the baseline", () => {
    const canvas = [block("a", { block_type: "code", code: "# a" })];
    setDraft(canvas);
    // Baseline captured while the YAML editor is closed.
    useWorkflowSnapshotStore.getState().captureSnapshot();

    useWorkflowYamlEditorStore.setState({
      active: true,
      entrySnapshot: toYaml({ parameters: [], blocks: canvas }),
      draft: toYaml({
        parameters: [],
        blocks: [...canvas, { label: "b", block_type: "code", code: "# b" }],
      }),
    });
    useWorkflowSnapshotStore.getState().noteDraftChange(true);

    expect(useWorkflowSnapshotStore.getState().contentDirty).toBe(true);
  });

  it("clears dirtiness when a user edit is reverted", () => {
    setDraft([block("a", { url: "x" })]);
    useWorkflowSnapshotStore.getState().captureSnapshot();

    setDraft([block("a", { url: "y" })]);
    useWorkflowSnapshotStore.getState().noteDraftChange(true);
    expect(useWorkflowSnapshotStore.getState().contentDirty).toBe(true);

    setDraft([block("a", { url: "x" })]);
    useWorkflowSnapshotStore.getState().noteDraftChange(true);
    expect(useWorkflowSnapshotStore.getState().contentDirty).toBe(false);
  });
});

describe("WorkflowSnapshotStore YAML-draft trigger", () => {
  const canvas = [block("a", { block_type: "code", code: "# a" })];
  const dirtyDraft = () =>
    toYaml({
      parameters: [],
      blocks: [...canvas, { label: "b", block_type: "code", code: "# b" }],
    });

  // The canvas dirtiness effect keys off constructSaveData, which a Code-editor
  // edit never changes — so without the draft subscription the dot stays off on
  // a YAML-only edit. This wires the trigger as FlowRenderer does: a YAML draft
  // change refreshes as a *user-driven* edit (noteDraftChange(true)).
  it("lights contentDirty when a YAML-only edit diverges from the baseline", () => {
    setDraft(canvas);
    useWorkflowSnapshotStore.getState().captureSnapshot(); // clean baseline
    useWorkflowYamlEditorStore
      .getState()
      .open(toYaml({ parameters: [], blocks: canvas }));

    const unsub = subscribeToYamlDraftChanges(() =>
      useWorkflowSnapshotStore.getState().noteDraftChange(true),
    );
    expect(useWorkflowSnapshotStore.getState().contentDirty).toBe(false);

    useWorkflowYamlEditorStore.getState().setDraft(dirtyDraft());
    unsub();

    expect(useWorkflowSnapshotStore.getState().contentDirty).toBe(true);
  });

  // Regression: the code editor's keystrokes never reach the canvas gesture
  // window, so classifying a YAML draft change by gesture proximity reads it as
  // non-user materialization and ABSORBS it into the baseline — dot stays dark.
  // The subscription must refresh as user-driven; this pins the failure mode.
  it("absorbs the edit (dot dark) if a YAML change is refreshed as non-user", () => {
    setDraft(canvas);
    useWorkflowSnapshotStore.getState().captureSnapshot();
    useWorkflowYamlEditorStore
      .getState()
      .open(toYaml({ parameters: [], blocks: canvas }));

    const unsub = subscribeToYamlDraftChanges(() =>
      useWorkflowSnapshotStore.getState().noteDraftChange(false),
    );
    useWorkflowYamlEditorStore.getState().setDraft(dirtyDraft());
    unsub();

    expect(useWorkflowSnapshotStore.getState().contentDirty).toBe(false);
  });
});

describe("WorkflowSnapshotStore cross-workflow reset (A→B same instance)", () => {
  // Workspace can be reused across workflows without remounting (A/build →
  // B/build), so its per-workflow reset effect must clear the baseline — a
  // carried snapshot diffs B's graph against A's and phantoms an "edited" line.
  it("a carried baseline phantoms the next workflow; clearSnapshot prevents it", () => {
    setDraft([block("a", { url: "wfA" })]);
    useWorkflowSnapshotStore.getState().captureSnapshot(); // baseline = workflow A

    setDraft([block("b", { url: "wfB" })]); // A→B nav: getSaveData now returns B
    const carried = useWorkflowSnapshotStore.getState().snapshot;
    expect(isDraftDirty(saveData([block("b", { url: "wfB" })]), carried)).toBe(
      true,
    ); // phantom without a reset

    useWorkflowSnapshotStore.getState().clearSnapshot(); // the per-workflow reset
    expect(useWorkflowSnapshotStore.getState().snapshot).toBeNull();
    expect(isDraftDirty(saveData([block("b", { url: "wfB" })]), null)).toBe(
      false,
    );
  });
});

describe("WorkflowSnapshotStore absorbs pre-first-edit materialization", () => {
  // The early-interaction phantom: a bare interaction freezes the baseline, then
  // login-credential autofill resolves and mutates the canvas. That change is
  // not user-driven and no user edit has happened, so it is absorbed into the
  // baseline instead of surfacing as a phantom "Edited" block.
  it("re-baselines a non-user change before any user edit (no phantom)", () => {
    setDraft([block("login", { navigation_goal: "" })]);
    useWorkflowSnapshotStore.getState().captureSnapshot();

    // Autofill writes the login goal — a change with no preceding gesture.
    setDraft([block("login", { navigation_goal: "log in with the context" })]);
    useWorkflowSnapshotStore.getState().noteDraftChange(false);

    const state = useWorkflowSnapshotStore.getState();
    expect(state.contentDirty).toBe(false);
    // The baseline moved to include the autofill, so the current draft is clean.
    expect(
      isDraftDirty(
        saveData([
          block("login", { navigation_goal: "log in with the context" }),
        ]),
        state.snapshot,
      ),
    ).toBe(false);
  });

  it("a user-driven change with no content diff still absorbs a later autofill", () => {
    // Selecting/dragging a node recomputes the graph (userDriven) without
    // changing content — it must not count as an edit, or a subsequent autofill
    // would surface as a phantom.
    setDraft([block("a", { url: "x" })]);
    useWorkflowSnapshotStore.getState().captureSnapshot();

    useWorkflowSnapshotStore.getState().noteDraftChange(true); // same draft
    expect(useWorkflowSnapshotStore.getState().userHasEdited).toBe(false);
    expect(useWorkflowSnapshotStore.getState().contentDirty).toBe(false);

    setDraft([
      block("a", { url: "x" }),
      block("b", { block_type: "login", navigation_goal: "log in" }),
    ]);
    useWorkflowSnapshotStore.getState().noteDraftChange(false);
    expect(useWorkflowSnapshotStore.getState().contentDirty).toBe(false);
  });

  it("does NOT absorb a non-user change once the user has edited", () => {
    setDraft([block("a", { url: "x" })]);
    useWorkflowSnapshotStore.getState().captureSnapshot();

    // A genuine user edit freezes the baseline.
    setDraft([block("a", { url: "y" })]);
    useWorkflowSnapshotStore.getState().noteDraftChange(true);
    expect(useWorkflowSnapshotStore.getState().userHasEdited).toBe(true);

    // A later non-user change is NOT absorbed — it surfaces (documented ceiling).
    setDraft([
      block("a", { url: "y" }),
      block("b", { block_type: "login", navigation_goal: "log in" }),
    ]);
    useWorkflowSnapshotStore.getState().noteDraftChange(false);
    expect(useWorkflowSnapshotStore.getState().contentDirty).toBe(true);
  });
});

describe("WorkflowSnapshotStore markUserEdit (Copilot / gesture-less edits)", () => {
  // A Copilot build lands async, outside the gesture window, so its change would
  // be classified non-user-driven. markUserEdit is the source-level signal that
  // keeps it from being absorbed, so the dot lights and the summary itemizes it.
  it("surfaces a later non-user change instead of absorbing it", () => {
    setDraft([block("a", { url: "x" })]);
    useWorkflowSnapshotStore.getState().captureSnapshot();

    useWorkflowSnapshotStore.getState().markUserEdit();
    expect(useWorkflowSnapshotStore.getState().userHasEdited).toBe(true);

    // Copilot mutates the canvas (no gesture) — must surface, not be absorbed.
    setDraft([
      block("a", { url: "x" }),
      block("b", { block_type: "code", code: "# copilot" }),
    ]);
    useWorkflowSnapshotStore.getState().noteDraftChange(false);
    expect(useWorkflowSnapshotStore.getState().contentDirty).toBe(true);
  });

  it("captures a pre-change baseline when a Copilot build is the first edit", () => {
    // No gesture yet → snapshot is null. markUserEdit freezes the current
    // (pre-Copilot) draft so the build diffs against it.
    expect(useWorkflowSnapshotStore.getState().snapshot).toBeNull();
    setDraft([block("a", { url: "x" })]);
    useWorkflowSnapshotStore.getState().markUserEdit();
    expect(useWorkflowSnapshotStore.getState().snapshot).not.toBeNull();

    setDraft([
      block("a", { url: "x" }),
      block("b", { block_type: "code", code: "# copilot" }),
    ]);
    useWorkflowSnapshotStore.getState().noteDraftChange(false);
    expect(useWorkflowSnapshotStore.getState().contentDirty).toBe(true);
  });
});
