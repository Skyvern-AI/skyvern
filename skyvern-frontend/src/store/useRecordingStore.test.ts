import { beforeEach, describe, expect, it } from "vitest";

import {
  upsertDraftSteps,
  useRecordingStore,
  type OptimisticActionKind,
  type OptimisticStep,
  type RecordingActionKind,
  type RecordingDraftStep,
  type RecordingInterpretationUpdate,
} from "./useRecordingStore";

let seq = 0;
function opt(kind: OptimisticActionKind = "click"): OptimisticStep {
  seq += 1;
  return {
    local_id: `o-${seq}`,
    action_kind: kind,
    title: `${kind} ${seq}`,
    timestamp: seq,
  };
}

function draft(id: string, kind: RecordingActionKind): RecordingDraftStep {
  return {
    step_id: id,
    action_kind: kind,
    block_type:
      kind === "url_change" ? "goto_url" : kind === "wait" ? "wait" : "action",
    label: id,
    status: "ready",
    editable_fields: [],
    parameters: [],
    parameter_keys: [],
  };
}

function update(
  id: string,
  revision: number,
  steps: Array<RecordingDraftStep>,
  opts: {
    pending?: boolean;
    finalized?: boolean;
    is_snapshot?: boolean;
    changed_steps?: Array<RecordingDraftStep>;
  } = {},
): RecordingInterpretationUpdate {
  return {
    interpretation_session_id: id,
    session_revision: revision,
    steps,
    pending: opts.pending ?? false,
    finalized: opts.finalized ?? false,
    ...(opts.is_snapshot !== undefined
      ? { is_snapshot: opts.is_snapshot }
      : {}),
    ...(opts.changed_steps !== undefined
      ? { changed_steps: opts.changed_steps }
      : {}),
  };
}

const store = () => useRecordingStore.getState();

beforeEach(() => {
  store().reset();
});

describe("addOptimisticStep", () => {
  it("appends while recording and unpaused", () => {
    store().setIsRecording(true);
    store().addOptimisticStep(opt());
    expect(store().optimisticSteps).toHaveLength(1);
  });

  it("no-ops when not recording", () => {
    store().addOptimisticStep(opt());
    expect(store().optimisticSteps).toHaveLength(0);
  });

  it("no-ops when capture is paused", () => {
    store().setIsRecording(true);
    store().setManualCapturePaused(true);
    store().addOptimisticStep(opt());
    expect(store().optimisticSteps).toHaveLength(0);
  });

  it("no-ops after finish is requested", () => {
    store().setIsRecording(true);
    store().requestFinish();
    store().addOptimisticStep(opt());
    expect(store().optimisticSteps).toHaveLength(0);
  });
});

describe("applyInterpretationUpdate: transient optimistic steps", () => {
  beforeEach(() => {
    store().setIsRecording(true);
  });

  it("keeps optimistic steps while interpretation is pending", () => {
    store().addOptimisticStep(opt());
    store().addOptimisticStep(opt());

    // Immediate pending update (steps unchanged) on the first significant event.
    store().applyInterpretationUpdate(update("s1", 1, [], { pending: true }));
    expect(store().optimisticSteps).toHaveLength(2);

    // Another pending bump keeps them.
    store().applyInterpretationUpdate(update("s1", 2, [], { pending: true }));
    expect(store().optimisticSteps).toHaveLength(2);
  });

  it("clears optimistic steps once interpretation settles (pending=false)", () => {
    store().addOptimisticStep(opt());
    store().addOptimisticStep(opt());

    store().applyInterpretationUpdate(
      update("s1", 1, [draft("s1-0", "click")], { pending: false }),
    );
    expect(store().optimisticSteps).toHaveLength(0);
  });

  it("does not strand placeholders for events the backend folds away", () => {
    // A link click yields a click (+wait) but no goto; the optimistic click must
    // still clear when interpretation settles rather than linger forever.
    store().addOptimisticStep(opt("click"));
    store().applyInterpretationUpdate(
      update("s1", 1, [draft("s1-0", "click"), draft("s1-1", "wait")], {
        pending: false,
      }),
    );
    expect(store().optimisticSteps).toHaveLength(0);
  });

  it("clears optimistic steps on a finalized snapshot", () => {
    store().addOptimisticStep(opt());
    store().applyInterpretationUpdate(update("s1", 1, [], { finalized: true }));
    expect(store().optimisticSteps).toHaveLength(0);
  });

  it("clears optimistic steps on a genuine session change", () => {
    store().addOptimisticStep(opt());
    store().applyInterpretationUpdate(update("s1", 1, [], { pending: true }));
    expect(store().optimisticSteps).toHaveLength(1);

    store().applyInterpretationUpdate(update("s2", 1, [], { pending: true }));
    expect(store().optimisticSteps).toHaveLength(0);
    expect(store().interpretationSessionId).toBe("s2");
  });
});

describe("commit safety and resets", () => {
  it("getFinalDraftSteps never includes optimistic steps", () => {
    store().setIsRecording(true);
    store().addOptimisticStep(opt());
    // pending=true so the optimistic step is still present alongside a draft step
    store().applyInterpretationUpdate(
      update("s1", 1, [draft("s1-0", "click")], { pending: true }),
    );

    const final = store().getFinalDraftSteps();
    expect(final).toHaveLength(1);
    expect(final?.every((s) => s.step_id === "s1-0")).toBe(true);
    expect(store().optimisticSteps).toHaveLength(1);
  });

  it("reset() and starting a new recording zero optimistic state", () => {
    store().setIsRecording(true);
    store().addOptimisticStep(opt());
    store().reset();
    expect(store().optimisticSteps).toHaveLength(0);

    store().setIsRecording(true);
    store().addOptimisticStep(opt());
    expect(store().optimisticSteps).toHaveLength(1);
  });

  it("mints a new recordingAttemptId for each new recording", () => {
    store().setIsRecording(true);
    const first = store().recordingAttemptId;
    expect(first).toBeTruthy();

    store().setIsRecording(false);
    store().setIsRecording(true);
    const second = store().recordingAttemptId;
    expect(second).toBeTruthy();
    expect(second).not.toBe(first);
  });
});

describe("upsertDraftSteps", () => {
  it("returns the same list when there are no changes", () => {
    const current = [draft("a", "click"), draft("b", "click")];
    expect(upsertDraftSteps(current, [])).toBe(current);
  });

  it("replaces a step in place, preserving order", () => {
    const current = [draft("a", "click"), draft("b", "click")];
    const updated = { ...draft("a", "click"), label: "updated" };
    const result = upsertDraftSteps(current, [updated]);
    expect(result.map((s) => s.step_id)).toEqual(["a", "b"]);
    expect(result[0]?.label).toBe("updated");
    expect(result[1]).toBe(current[1]);
  });

  it("appends genuinely-new steps in arrival order", () => {
    const current = [draft("a", "click")];
    const result = upsertDraftSteps(current, [
      draft("b", "click"),
      draft("c", "click"),
    ]);
    expect(result.map((s) => s.step_id)).toEqual(["a", "b", "c"]);
  });

  it("handles a mix of replace and append in one call", () => {
    const current = [draft("a", "click"), draft("b", "click")];
    const result = upsertDraftSteps(current, [
      { ...draft("b", "click"), label: "b2" },
      draft("c", "click"),
    ]);
    expect(result.map((s) => s.step_id)).toEqual(["a", "b", "c"]);
    expect(result[1]?.label).toBe("b2");
  });

  it("is idempotent for a repeated step id", () => {
    const current = [draft("a", "click")];
    const result = upsertDraftSteps(current, [
      draft("a", "click"),
      draft("a", "click"),
    ]);
    expect(result.map((s) => s.step_id)).toEqual(["a"]);
  });
});

describe("applyInterpretationUpdate: snapshot vs delta", () => {
  beforeEach(() => {
    store().setIsRecording(true);
  });

  it("legacy message with no is_snapshot/changed_steps replaces wholesale", () => {
    store().applyInterpretationUpdate(
      update("s1", 1, [draft("s1-0", "click"), draft("s1-1", "click")]),
    );
    expect(store().draftSteps.map((s) => s.step_id)).toEqual(["s1-0", "s1-1"]);

    // A later snapshot fully replaces (does not merge).
    store().applyInterpretationUpdate(
      update("s1", 2, [draft("s1-2", "click")]),
    );
    expect(store().draftSteps.map((s) => s.step_id)).toEqual(["s1-2"]);
  });

  it("delta upserts changed steps into the current list", () => {
    // Seed with a snapshot of two interpreting steps.
    const a = { ...draft("s1-0", "click"), status: "interpreting" as const };
    const b = { ...draft("s1-1", "click"), status: "interpreting" as const };
    store().applyInterpretationUpdate(
      update("s1", 1, [a, b], { pending: true }),
    );

    // Delta: s1-0 flips to ready with a real label; s1-1 untouched.
    const enriched = { ...draft("s1-0", "click"), label: "Click Vancouver" };
    store().applyInterpretationUpdate(
      update("s1", 2, [], {
        pending: true,
        is_snapshot: false,
        changed_steps: [enriched],
      }),
    );

    const steps = store().draftSteps;
    expect(steps.map((s) => s.step_id)).toEqual(["s1-0", "s1-1"]);
    expect(steps[0]?.status).toBe("ready");
    expect(steps[0]?.label).toBe("Click Vancouver");
    expect(steps[1]?.status).toBe("interpreting");
  });

  it("delta appends a brand-new step", () => {
    store().applyInterpretationUpdate(
      update("s1", 1, [draft("s1-0", "click")], { pending: true }),
    );
    store().applyInterpretationUpdate(
      update("s1", 2, [], {
        pending: true,
        is_snapshot: false,
        changed_steps: [draft("s1-1", "click")],
      }),
    );
    expect(store().draftSteps.map((s) => s.step_id)).toEqual(["s1-0", "s1-1"]);
  });

  it("ignores a stale delta at or below the current revision", () => {
    store().applyInterpretationUpdate(
      update("s1", 5, [draft("s1-0", "click")], { pending: true }),
    );
    store().applyInterpretationUpdate(
      update("s1", 5, [], {
        pending: true,
        is_snapshot: false,
        changed_steps: [draft("s1-9", "click")],
      }),
    );
    expect(store().draftSteps.map((s) => s.step_id)).toEqual(["s1-0"]);
  });
});
