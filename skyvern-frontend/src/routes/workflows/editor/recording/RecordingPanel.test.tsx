// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useRecordedBlocksStore } from "@/store/RecordedBlocksStore";
import { useRecordingStore } from "@/store/useRecordingStore";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";

import { RecordingPanel } from "./RecordingPanel";

const mutateMock = vi.fn();

vi.mock("@/routes/browserSessions/hooks/useProcessRecordingMutation", () => ({
  useProcessRecordingMutation: () => ({
    isPending: false,
    isError: false,
    mutate: mutateMock,
  }),
}));

const initialRecording = useRecordingStore.getState();
const initialPanel = useWorkflowPanelStore.getState();
const initialRecordedBlocks = useRecordedBlocksStore.getState();

describe("RecordingPanel", () => {
  beforeEach(() => {
    HTMLElement.prototype.scrollIntoView = vi.fn();
    mutateMock.mockReset();
    useRecordingStore.setState(initialRecording, true);
    useWorkflowPanelStore.setState(initialPanel, true);
    useRecordedBlocksStore.setState(initialRecordedBlocks, true);

    // Give the panel a valid insertion point so insertionPointMissing can't
    // confound the browserSessionId assertions below.
    useWorkflowPanelStore.setState({
      workflowPanelState: {
        active: true,
        content: "nodeLibrary",
        data: {
          previous: null,
          next: null,
          parent: undefined,
          connectingEdgeType: "default",
        },
      },
    });
  });

  afterEach(() => {
    cleanup();
  });

  it("keeps Done disabled and never calls process_recording while the browser session id has not resolved", () => {
    render(<RecordingPanel browserSessionId={null} />);

    const doneButton = screen.getByRole("button", {
      name: /done/i,
    }) as HTMLButtonElement;
    expect(doneButton.disabled).toBe(true);

    fireEvent.click(doneButton);

    expect(mutateMock).not.toHaveBeenCalled();
  });

  it("enables Done and processes the recording once the browser session id resolves", () => {
    const { rerender } = render(<RecordingPanel browserSessionId={null} />);

    expect(
      (screen.getByRole("button", { name: /done/i }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);

    rerender(<RecordingPanel browserSessionId="pbs_123" />);

    const doneButton = screen.getByRole("button", {
      name: /done/i,
    }) as HTMLButtonElement;
    expect(doneButton.disabled).toBe(false);

    fireEvent.click(doneButton);

    expect(mutateMock).toHaveBeenCalledTimes(1);
  });

  it("retries the finalize commit once the browser session id resolves again after briefly going missing mid-finish", () => {
    vi.useFakeTimers();
    try {
      // Non-zero so the finalize effect waits on FINALIZE_TIMEOUT_MS instead
      // of committing immediately.
      useRecordingStore.setState({ sessionRevision: 1 });

      const { rerender } = render(
        <RecordingPanel browserSessionId="pbs_123" />,
      );

      fireEvent.click(screen.getByRole("button", { name: /done/i }));

      // The debug session's browser_session_id blips to null while the
      // finalize timeout is pending.
      rerender(<RecordingPanel browserSessionId={null} />);
      vi.advanceTimersByTime(5000);
      expect(mutateMock).not.toHaveBeenCalled();

      // It resolves again; the finalize wait should retry rather than
      // leaving the panel stuck on "Finishing recording".
      rerender(<RecordingPanel browserSessionId="pbs_123" />);
      vi.advanceTimersByTime(5000);

      expect(mutateMock).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });
});
