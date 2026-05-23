// @vitest-environment jsdom

import { beforeEach, describe, expect, test, vi } from "vitest";

import { useWorkflowHistoryAccessStore } from "./WorkflowHistoryAccessStore";

beforeEach(() => {
  useWorkflowHistoryAccessStore.getState().reset();
});

describe("WorkflowHistoryAccessStore", () => {
  test("initial state has both flags false and noop callbacks", () => {
    const state = useWorkflowHistoryAccessStore.getState();
    expect(state.canUndo).toBe(false);
    expect(state.canRedo).toBe(false);
    expect(typeof state.undo).toBe("function");
    expect(typeof state.redo).toBe("function");
    expect(typeof state.captureImmediately).toBe("function");
  });

  test("initial undo/redo/captureImmediately are safe no-ops", () => {
    const state = useWorkflowHistoryAccessStore.getState();
    expect(() => state.undo()).not.toThrow();
    expect(() => state.redo()).not.toThrow();
    expect(() => state.captureImmediately()).not.toThrow();
  });

  test("setHistoryAccess publishes the full snapshot", () => {
    const undo = vi.fn();
    const redo = vi.fn();
    const captureImmediately = vi.fn();

    useWorkflowHistoryAccessStore.getState().setHistoryAccess({
      canUndo: true,
      canRedo: true,
      undo,
      redo,
      captureImmediately,
    });

    const state = useWorkflowHistoryAccessStore.getState();
    expect(state.canUndo).toBe(true);
    expect(state.canRedo).toBe(true);
    state.undo();
    state.redo();
    state.captureImmediately();
    expect(undo).toHaveBeenCalledTimes(1);
    expect(redo).toHaveBeenCalledTimes(1);
    expect(captureImmediately).toHaveBeenCalledTimes(1);
  });

  test("reset returns to initial state", () => {
    useWorkflowHistoryAccessStore.getState().setHistoryAccess({
      canUndo: true,
      canRedo: true,
      undo: vi.fn(),
      redo: vi.fn(),
      captureImmediately: vi.fn(),
    });
    useWorkflowHistoryAccessStore.getState().reset();
    expect(useWorkflowHistoryAccessStore.getState().canUndo).toBe(false);
    expect(useWorkflowHistoryAccessStore.getState().canRedo).toBe(false);
  });
});
