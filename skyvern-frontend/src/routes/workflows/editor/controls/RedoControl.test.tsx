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

import { RedoControl } from "./RedoControl";

describe("RedoControl", () => {
  const redo = vi.fn();

  beforeEach(() => {
    useWorkflowHistoryAccessStore.getState().setHistoryAccess({
      canUndo: false,
      canRedo: false,
      undo: vi.fn(),
      redo,
      captureImmediately: vi.fn(),
    });
    useRecordingStore.setState({ isRecording: false });
  });

  afterEach(() => {
    cleanup();
    redo.mockReset();
    useWorkflowHistoryAccessStore.getState().reset();
  });

  test("is collapsed (aria-hidden) when canRedo is false", () => {
    const { container } = render(<RedoControl />);
    const wrapper = container.firstElementChild as HTMLElement;
    expect(wrapper.getAttribute("aria-hidden")).toBe("true");
  });

  test("is revealed (aria-hidden=false) when canRedo flips to true", () => {
    const { container } = render(<RedoControl />);
    act(() => {
      useWorkflowHistoryAccessStore.getState().setHistoryAccess({
        canUndo: false,
        canRedo: true,
        undo: vi.fn(),
        redo,
        captureImmediately: vi.fn(),
      });
    });
    const wrapper = container.firstElementChild as HTMLElement;
    expect(wrapper.getAttribute("aria-hidden")).toBe("false");
  });

  test("clicking the button fires redo", () => {
    useWorkflowHistoryAccessStore.getState().setHistoryAccess({
      canUndo: false,
      canRedo: true,
      undo: vi.fn(),
      redo,
      captureImmediately: vi.fn(),
    });
    render(<RedoControl />);
    fireEvent.click(screen.getByRole("button", { name: /redo/i }));
    expect(redo).toHaveBeenCalledTimes(1);
  });

  test("button is disabled while recording even when canRedo is true", () => {
    useWorkflowHistoryAccessStore.getState().setHistoryAccess({
      canUndo: false,
      canRedo: true,
      undo: vi.fn(),
      redo,
      captureImmediately: vi.fn(),
    });
    useRecordingStore.setState({ isRecording: true });
    render(<RedoControl />);
    const btn = screen.getByRole("button", { name: /redo/i });
    expect(btn.hasAttribute("disabled")).toBe(true);
    fireEvent.click(btn);
    expect(redo).not.toHaveBeenCalled();
  });
});
