import { act, cleanup, fireEvent, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { computeFollowSignature, useStickToBottom } from "./useStickToBottom";
import {
  EMPTY_NARRATIVE,
  TurnNarrativeState,
  applyNarrativeEvent,
} from "./narrativeState";
import {
  WorkflowCopilotBlockProgressUpdate,
  WorkflowCopilotNarrationUpdate,
  WorkflowCopilotTurnStartUpdate,
  WorkflowCopilotWorkflowDraftUpdate,
} from "./workflowCopilotTypes";

const turnStart = (): WorkflowCopilotTurnStartUpdate => ({
  type: "turn_start",
  turn_id: "turn-1",
  turn_index: 0,
  mode: "build",
  timestamp: "2026-05-25T00:00:00Z",
});

const workflowDraft = (
  overrides: Partial<WorkflowCopilotWorkflowDraftUpdate> = {},
): WorkflowCopilotWorkflowDraftUpdate => ({
  type: "workflow_draft",
  block_count: 2,
  block_labels: ["block_one", "block_two"],
  summary: "two block workflow",
  timestamp: "2026-05-25T00:00:03Z",
  ...overrides,
});

const blockProgress = (
  overrides: Partial<WorkflowCopilotBlockProgressUpdate> &
    Pick<WorkflowCopilotBlockProgressUpdate, "block_label" | "status">,
): WorkflowCopilotBlockProgressUpdate => ({
  type: "block_progress",
  workflow_run_block_id: `wrb_${overrides.block_label}`,
  block_type: "task",
  iteration: 0,
  timestamp: "2026-05-25T00:00:04Z",
  ...overrides,
});

const narration = (text: string): WorkflowCopilotNarrationUpdate => ({
  type: "narration",
  narration: text,
  iteration: 0,
  timestamp: "2026-05-25T00:00:04Z",
});

const sig = (narrative: TurnNarrativeState) =>
  computeFollowSignature([], narrative, false, false, null, false);

describe("computeFollowSignature", () => {
  it("changes when a block is appended", () => {
    const a = applyNarrativeEvent(EMPTY_NARRATIVE, turnStart());
    const b = applyNarrativeEvent(
      a,
      blockProgress({ block_label: "one", status: "running" }),
    );
    expect(sig(a)).not.toBe(sig(b));
  });

  it("changes when activity grows on a running block", () => {
    let s = applyNarrativeEvent(EMPTY_NARRATIVE, turnStart());
    s = applyNarrativeEvent(
      s,
      blockProgress({ block_label: "one", status: "running" }),
    );
    const before = sig(s);
    s = applyNarrativeEvent(s, narration("looking at the page"));
    expect(sig(s)).not.toBe(before);
  });

  it("keeps changing after the activity cap is reached (rotated tail)", () => {
    let s = applyNarrativeEvent(EMPTY_NARRATIVE, turnStart());
    s = applyNarrativeEvent(
      s,
      blockProgress({ block_label: "one", status: "running" }),
    );
    // Fill well past MAX_ACTIVITY_ENTRIES so length is pinned at the cap.
    for (let i = 0; i < 40; i++) {
      s = applyNarrativeEvent(s, narration(`step ${i}`));
    }
    const atCap = sig(s);
    const cappedBlock = s.blocks[0];
    expect(cappedBlock).toBeDefined();
    const cappedLength = cappedBlock!.activity.length;
    s = applyNarrativeEvent(s, narration("one more step"));
    // Length is unchanged at the cap, but the signature still moves.
    expect(s.blocks[0]!.activity.length).toBe(cappedLength);
    expect(sig(s)).not.toBe(atCap);
  });

  it("changes when a draft replaces labels at the same block count", () => {
    let s = applyNarrativeEvent(EMPTY_NARRATIVE, turnStart());
    s = applyNarrativeEvent(s, workflowDraft());
    const before = sig(s);
    s = applyNarrativeEvent(
      s,
      workflowDraft({ block_labels: ["renamed_a", "renamed_b"] }),
    );
    expect(sig(s)).not.toBe(before);
  });

  it("changes when the turn reaches a terminal response", () => {
    let s = applyNarrativeEvent(EMPTY_NARRATIVE, turnStart());
    const before = sig(s);
    s = applyNarrativeEvent(s, {
      type: "response",
      workflow_copilot_chat_id: "chat-1",
      message: "Done.",
      response_time: "2026-05-25T00:00:05Z",
      proposal_disposition: "no_proposal",
    });
    expect(sig(s)).not.toBe(before);
  });

  it("changes on a new message, loading, queued-prompt, and proposal state", () => {
    const base = computeFollowSignature(
      [],
      EMPTY_NARRATIVE,
      false,
      false,
      null,
      false,
    );
    expect(
      computeFollowSignature(
        [{ id: "m1", content: "hi" }],
        EMPTY_NARRATIVE,
        false,
        false,
        null,
        false,
      ),
    ).not.toBe(base);
    expect(
      computeFollowSignature([], EMPTY_NARRATIVE, true, false, null, false),
    ).not.toBe(base);
    expect(
      computeFollowSignature(
        [],
        EMPTY_NARRATIVE,
        false,
        false,
        { id: "q1", reason: "working" },
        false,
      ),
    ).not.toBe(base);
    expect(
      computeFollowSignature([], EMPTY_NARRATIVE, false, false, null, true),
    ).not.toBe(base);
  });

  it("is stable when nothing visible changed", () => {
    const s = applyNarrativeEvent(EMPTY_NARRATIVE, turnStart());
    expect(sig(s)).toBe(sig(s));
  });
});

// renderHook doesn't mount the ref onto a real node, so attach the test
// element to scrollRef.current by hand (RefObject.current is read-only).
function setRefCurrent<T>(ref: { current: T | null }, value: T) {
  ref.current = value;
}

function makeScrollEl(opts: {
  scrollHeight: number;
  clientHeight: number;
  scrollTop: number;
}) {
  const el = document.createElement("div");
  Object.defineProperty(el, "scrollHeight", {
    configurable: true,
    value: opts.scrollHeight,
  });
  Object.defineProperty(el, "clientHeight", {
    configurable: true,
    value: opts.clientHeight,
  });
  let top = opts.scrollTop;
  Object.defineProperty(el, "scrollTop", {
    configurable: true,
    get: () => top,
    set: (v: number) => {
      top = v;
    },
  });
  el.scrollTo = vi.fn((arg: { top: number }) => {
    top = arg.top;
  }) as unknown as typeof el.scrollTo;
  document.body.appendChild(el);
  return el;
}

afterEach(() => {
  cleanup();
  document.body.innerHTML = "";
});

describe("useStickToBottom", () => {
  it("starts pinned and follows on content change", () => {
    const el = makeScrollEl({
      scrollHeight: 1000,
      clientHeight: 300,
      scrollTop: 700,
    });
    const { result, rerender } = renderHook(
      ({ signature }) => {
        const hook = useStickToBottom<HTMLDivElement>(signature, {
          enabled: true,
        });
        setRefCurrent(hook.scrollRef, el);
        return hook;
      },
      { initialProps: { signature: "a" } },
    );
    expect(result.current.isPinned).toBe(true);
    (el.scrollTo as ReturnType<typeof vi.fn>).mockClear();
    rerender({ signature: "b" });
    expect(el.scrollTo).toHaveBeenCalled();
  });

  it("disengages when the user scrolls up past the threshold", () => {
    const el = makeScrollEl({
      scrollHeight: 1000,
      clientHeight: 300,
      scrollTop: 700,
    });
    const { result } = renderHook(() => {
      const hook = useStickToBottom<HTMLDivElement>("a", { enabled: true });
      setRefCurrent(hook.scrollRef, el);
      return hook;
    });
    el.scrollTop = 500; // distance = 1000 - 500 - 300 = 200 > 48
    fireEvent.scroll(el);
    expect(result.current.isPinned).toBe(false);
  });

  it("does not follow new content while disengaged", () => {
    const el = makeScrollEl({
      scrollHeight: 1000,
      clientHeight: 300,
      scrollTop: 700,
    });
    const { result, rerender } = renderHook(
      ({ signature }) => {
        const hook = useStickToBottom<HTMLDivElement>(signature, {
          enabled: true,
        });
        setRefCurrent(hook.scrollRef, el);
        return hook;
      },
      { initialProps: { signature: "a" } },
    );
    el.scrollTop = 500;
    fireEvent.scroll(el);
    expect(result.current.isPinned).toBe(false);
    (el.scrollTo as ReturnType<typeof vi.fn>).mockClear();
    rerender({ signature: "b" });
    expect(el.scrollTo).not.toHaveBeenCalled();
  });

  it("re-engages when the user scrolls back to the bottom", () => {
    const el = makeScrollEl({
      scrollHeight: 1000,
      clientHeight: 300,
      scrollTop: 700,
    });
    const { result } = renderHook(() => {
      const hook = useStickToBottom<HTMLDivElement>("a", { enabled: true });
      setRefCurrent(hook.scrollRef, el);
      return hook;
    });
    el.scrollTop = 500;
    fireEvent.scroll(el);
    expect(result.current.isPinned).toBe(false);
    el.scrollTop = 700; // distance = 0 <= 48
    fireEvent.scroll(el);
    expect(result.current.isPinned).toBe(true);
  });

  it("jumpToLatest re-pins and scrolls to the bottom", () => {
    const el = makeScrollEl({
      scrollHeight: 1000,
      clientHeight: 300,
      scrollTop: 700,
    });
    const { result } = renderHook(() => {
      const hook = useStickToBottom<HTMLDivElement>("a", { enabled: true });
      setRefCurrent(hook.scrollRef, el);
      return hook;
    });
    el.scrollTop = 500;
    fireEvent.scroll(el);
    expect(result.current.isPinned).toBe(false);
    (el.scrollTo as ReturnType<typeof vi.fn>).mockClear();
    act(() => {
      result.current.jumpToLatest();
    });
    expect(result.current.isPinned).toBe(true);
    expect(el.scrollTo).toHaveBeenCalled();
  });

  it("re-pins and scrolls to the bottom after a close/reopen following a scroll-up", () => {
    const el = makeScrollEl({
      scrollHeight: 1000,
      clientHeight: 300,
      scrollTop: 700,
    });
    const { result, rerender } = renderHook(
      ({ enabled }) => {
        const hook = useStickToBottom<HTMLDivElement>("a", { enabled });
        setRefCurrent(hook.scrollRef, el);
        return hook;
      },
      { initialProps: { enabled: true } },
    );
    el.scrollTop = 500;
    fireEvent.scroll(el);
    expect(result.current.isPinned).toBe(false);
    rerender({ enabled: false });
    (el.scrollTo as ReturnType<typeof vi.fn>).mockClear();
    rerender({ enabled: true });
    expect(result.current.isPinned).toBe(true);
    // Must actually scroll, not just flip the flag: the container remounts at
    // the top on reopen, so a flag-only re-pin would leave the user stuck up.
    expect(el.scrollTo).toHaveBeenCalled();
  });
});
