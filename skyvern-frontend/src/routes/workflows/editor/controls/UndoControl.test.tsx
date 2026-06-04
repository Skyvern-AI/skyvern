// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { useWorkflowHistoryAccessStore } from "@/store/WorkflowHistoryAccessStore";
import { useRecordingStore } from "@/store/useRecordingStore";

import { UndoControl } from "./UndoControl";

describe("UndoControl", () => {
  const undo = vi.fn();

  beforeEach(() => {
    useWorkflowHistoryAccessStore.getState().setHistoryAccess({
      canUndo: false,
      canRedo: false,
      undo,
      redo: vi.fn(),
      captureImmediately: vi.fn(),
    });
    useRecordingStore.setState({ isRecording: false });
  });

  afterEach(() => {
    cleanup();
    undo.mockReset();
    useWorkflowHistoryAccessStore.getState().reset();
  });

  test("is collapsed (aria-hidden) when canUndo is false", () => {
    const { container } = render(<UndoControl />);
    const wrapper = container.firstElementChild as HTMLElement;
    expect(wrapper.getAttribute("aria-hidden")).toBe("true");
  });

  test("is revealed (aria-hidden=false) when canUndo flips to true", () => {
    const { container } = render(<UndoControl />);
    act(() => {
      useWorkflowHistoryAccessStore.getState().setHistoryAccess({
        canUndo: true,
        canRedo: false,
        undo,
        redo: vi.fn(),
        captureImmediately: vi.fn(),
      });
    });
    const wrapper = container.firstElementChild as HTMLElement;
    expect(wrapper.getAttribute("aria-hidden")).toBe("false");
  });

  test("clicking the button fires undo", () => {
    useWorkflowHistoryAccessStore.getState().setHistoryAccess({
      canUndo: true,
      canRedo: false,
      undo,
      redo: vi.fn(),
      captureImmediately: vi.fn(),
    });
    render(<UndoControl />);
    fireEvent.click(screen.getByRole("button", { name: /undo/i }));
    expect(undo).toHaveBeenCalledTimes(1);
  });

  test("button is disabled while recording even when canUndo is true", () => {
    useWorkflowHistoryAccessStore.getState().setHistoryAccess({
      canUndo: true,
      canRedo: false,
      undo,
      redo: vi.fn(),
      captureImmediately: vi.fn(),
    });
    useRecordingStore.setState({ isRecording: true });
    render(<UndoControl />);
    const btn = screen.getByRole("button", { name: /undo/i });
    expect(btn.hasAttribute("disabled")).toBe(true);
    fireEvent.click(btn);
    expect(undo).not.toHaveBeenCalled();
  });
});
