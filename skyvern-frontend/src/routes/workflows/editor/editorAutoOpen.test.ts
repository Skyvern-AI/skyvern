import { describe, expect, it } from "vitest";

import {
  initialEditorAutoOpenState,
  shouldAutoOpenEditor,
} from "./editorAutoOpen";

describe("initialEditorAutoOpenState", () => {
  it("arms when the agent starts empty", () => {
    expect(initialEditorAutoOpenState(0)).toEqual({ armed: true });
  });

  it("does not arm when the agent already has blocks", () => {
    expect(initialEditorAutoOpenState(3)).toEqual({ armed: false });
  });
});

describe("shouldAutoOpenEditor", () => {
  const armed = { armed: true };
  const disarmed = { armed: false };

  it("fires on the first applied build that lands blocks in studio", () => {
    const result = shouldAutoOpenEditor(armed, {
      embedded: true,
      applied: true,
      blockCount: 2,
    });
    expect(result.fire).toBe(true);
    expect(result.nextState).toEqual({ armed: false });
  });

  it("does not fire for a draft (applied undefined)", () => {
    const result = shouldAutoOpenEditor(armed, {
      embedded: true,
      applied: undefined,
      blockCount: 2,
    });
    expect(result.fire).toBe(false);
    expect(result.nextState).toEqual(armed);
  });

  it("does not fire for a snap-back (applied false)", () => {
    const result = shouldAutoOpenEditor(armed, {
      embedded: true,
      applied: false,
      blockCount: 2,
    });
    expect(result.fire).toBe(false);
    expect(result.nextState).toEqual(armed);
  });

  it("does not fire outside the studio shell (embedded false)", () => {
    const result = shouldAutoOpenEditor(armed, {
      embedded: false,
      applied: true,
      blockCount: 2,
    });
    expect(result.fire).toBe(false);
    expect(result.nextState).toEqual(armed);
  });

  it("does not fire when the applied update lands zero blocks", () => {
    const result = shouldAutoOpenEditor(armed, {
      embedded: true,
      applied: true,
      blockCount: 0,
    });
    expect(result.fire).toBe(false);
    expect(result.nextState).toEqual(armed);
  });

  it("never fires when the agent already had blocks (started disarmed)", () => {
    const result = shouldAutoOpenEditor(disarmed, {
      embedded: true,
      applied: true,
      blockCount: 2,
    });
    expect(result.fire).toBe(false);
    expect(result.nextState).toEqual(disarmed);
  });

  it("fires exactly once across a sequence of applied builds", () => {
    let state = initialEditorAutoOpenState(0);

    const first = shouldAutoOpenEditor(state, {
      embedded: true,
      applied: true,
      blockCount: 1,
    });
    expect(first.fire).toBe(true);
    state = first.nextState;

    const second = shouldAutoOpenEditor(state, {
      embedded: true,
      applied: true,
      blockCount: 4,
    });
    expect(second.fire).toBe(false);
    expect(second.nextState).toEqual({ armed: false });
  });

  it("stays disarmed after firing regardless of the user closing the pane", () => {
    // This state has no notion of pane-open/closed — re-firing the disarmed
    // state it produces is the only way "after close" could matter, and it
    // still doesn't fire.
    const afterFire = shouldAutoOpenEditor(armed, {
      embedded: true,
      applied: true,
      blockCount: 1,
    }).nextState;

    const afterUserClosedPane = shouldAutoOpenEditor(afterFire, {
      embedded: true,
      applied: true,
      blockCount: 5,
    });

    expect(afterUserClosedPane.fire).toBe(false);
  });
});
