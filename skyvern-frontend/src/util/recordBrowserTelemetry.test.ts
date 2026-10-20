import posthog from "posthog-js";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useRecordingStore } from "@/store/useRecordingStore";

import {
  captureRecordBrowser,
  captureRecordBrowserUndoAfterRecordingIfRecent,
  markRecordBrowserProcessed,
} from "./recordBrowserTelemetry";

vi.mock("posthog-js", () => ({ default: { capture: vi.fn() } }));

const store = useRecordingStore.getState;
const captured = () => vi.mocked(posthog.capture).mock.calls;

describe("record browser telemetry correlation", () => {
  afterEach(() => {
    store().reset();
    vi.mocked(posthog.capture).mockClear();
  });

  it("stamps every lifecycle event with the attempt, workflow, and browser session", () => {
    store().setIsRecording(true, {
      workflowPermanentId: "wpid-1",
      browserSessionId: "pbs-1",
    });
    store().deleteDraftStep("step-1");
    store().patchDraftStep("step-2", { title: "Edited" });
    store().reset();

    expect(captured().map(([event]) => event)).toEqual([
      "record_browser.started",
      "record_browser.draft_step_deleted",
      "record_browser.draft_step_edited",
      "record_browser.abandoned",
    ]);
    const attemptId = store().recordingAttemptId;
    expect(attemptId).toEqual(expect.any(String));
    for (const [, properties] of captured()) {
      expect(properties).toMatchObject({
        recording_attempt_id: attemptId,
        workflow_permanent_id: "wpid-1",
        browser_session_id: "pbs-1",
      });
    }

    vi.mocked(posthog.capture).mockClear();
    captureRecordBrowser("record_browser.missing_insertion_point");
    expect(captured()[0]?.[1]).not.toHaveProperty("recording_attempt_id");

    store().setIsRecording(true, {
      workflowPermanentId: "wpid-1",
      browserSessionId: "pbs-1",
    });
    expect(store().recordingAttemptId).not.toBe(attemptId);
  });

  it("attributes an undo to the processed attempt after a new attempt starts", () => {
    store().setIsRecording(true, {
      workflowPermanentId: "wpid-a",
      browserSessionId: "pbs-a",
    });
    const processedAttemptId = store().recordingAttemptId;
    markRecordBrowserProcessed(2);
    store().setIsRecording(false);
    store().setIsRecording(true, {
      workflowPermanentId: "wpid-b",
      browserSessionId: "pbs-b",
    });
    vi.mocked(posthog.capture).mockClear();

    captureRecordBrowserUndoAfterRecordingIfRecent(2);

    expect(captured()).toEqual([
      [
        "record_browser.undo_after_recording",
        expect.objectContaining({
          recording_attempt_id: processedAttemptId,
          workflow_permanent_id: "wpid-a",
          browser_session_id: "pbs-a",
        }),
      ],
    ]);
  });
});
