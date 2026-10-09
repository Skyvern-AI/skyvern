// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import posthog from "posthog-js";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useRecordingFeedbackStore } from "@/store/RecordingFeedbackStore";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import { setRecordBrowserContext } from "@/util/recordBrowserTelemetry";

import { RecordingFeedbackPrompt } from "./RecordingFeedbackPrompt";

const post = vi.hoisted(() => vi.fn());

vi.mock("posthog-js", () => ({ default: { capture: vi.fn() } }));
vi.mock("@/api/AxiosClient", () => ({ getClient: async () => ({ post }) }));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));
vi.mock("@/hooks/useUser", () => ({
  useUser: () => ({ get: () => ({ email: "user@example.com" }) }),
}));

const ALLOWED_KEYS = [
  "recording_attempt_id",
  "recording_id",
  "workflow_permanent_id",
  "browser_session_id",
  "rating",
  "reason_code",
  "has_reason",
  "updated",
];

async function click(name: string, role = "button") {
  await act(async () => fireEvent.click(screen.getByRole(role, { name })));
}

const feedbackEvents = () =>
  vi
    .mocked(posthog.capture)
    .mock.calls.filter(
      ([event]) => event === "record_browser.feedback_submitted",
    )
    .map(([, properties]) => properties);

function renderPrompt() {
  render(<RecordingFeedbackPrompt workflowPermanentId="wpid-1" />);
}

describe("RecordingFeedbackPrompt", () => {
  afterEach(() => {
    cleanup();
    setRecordBrowserContext({});
    useRecordingFeedbackStore.setState({ target: null });
    vi.mocked(posthog.capture).mockClear();
    post.mockReset();
  });

  it("stores typed reasons in the feedback table, sends analytics only categories, and flags changes as updates", async () => {
    useRecordingFeedbackStore.getState().show({
      recording_attempt_id: "rra-1",
      recording_id: "br-1",
      workflow_permanent_id: "wpid-1",
      browser_session_id: "pbs-1",
    });
    // A newer attempt's live context must not relabel feedback for the processed one.
    setRecordBrowserContext({
      recording_attempt_id: "rra-2",
      workflow_permanent_id: "wpid-1",
      browser_session_id: "pbs-2",
    });
    renderPrompt();

    await click("Thumbs up");
    await click("Thumbs down");
    await click("Other", "radio");
    fireEvent.change(
      screen.getByRole("textbox", { name: "Describe what went wrong" }),
      { target: { value: "  Skipped the export step  " } },
    );
    await click("Send");

    const ids = {
      recording_attempt_id: "rra-1",
      recording_id: "br-1",
      workflow_permanent_id: "wpid-1",
      browser_session_id: "pbs-1",
    };
    expect(feedbackEvents()).toEqual([
      {
        ...ids,
        rating: "up",
        reason_code: undefined,
        has_reason: false,
        updated: false,
      },
      {
        ...ids,
        rating: "down",
        reason_code: undefined,
        has_reason: false,
        updated: true,
      },
      {
        ...ids,
        rating: "down",
        reason_code: "other",
        has_reason: true,
        updated: true,
      },
    ]);
    expect(JSON.stringify(feedbackEvents())).not.toContain("export step");
    expect(post).toHaveBeenLastCalledWith("/feedback", {
      target_type: "browser_recording",
      target_id: "br-1",
      rating: "down",
      reason: "Skipped the export step",
      submitted_by: "user@example.com",
    });
    for (const properties of feedbackEvents()) {
      expect(
        Object.keys(properties ?? {}).every((key) =>
          ALLOWED_KEYS.includes(key),
        ),
      ).toBe(true);
    }
  });

  it("accepts a reasonless thumbs down without identifiers, and retires on dismiss but not on a Copilot persist", async () => {
    useRecordingFeedbackStore
      .getState()
      .show({ workflow_permanent_id: "wpid-1" });
    renderPrompt();

    await click("Thumbs down");
    await click("Skip");
    expect(post).not.toHaveBeenCalled();
    expect(feedbackEvents()).toEqual([
      expect.objectContaining({
        recording_attempt_id: undefined,
        recording_id: undefined,
        rating: "down",
        reason_code: undefined,
        updated: false,
      }),
    ]);

    await click("Dismiss recording feedback");
    expect(screen.queryByTestId("recording-feedback-prompt")).toBeNull();

    act(() =>
      useRecordingFeedbackStore
        .getState()
        .show({ workflow_permanent_id: "wpid-1" }),
    );
    expect(screen.getByTestId("recording-feedback-prompt")).toBeTruthy();
    // A Copilot persist (retitle, auto-accept) bumps the save count but is not the user's save.
    act(() =>
      useWorkflowHasChangesStore.getState().recordPersistedSave("wpid-1"),
    );
    expect(screen.getByTestId("recording-feedback-prompt")).toBeTruthy();
    expect(feedbackEvents()).toHaveLength(1);
  });

  it("does not count a response whose save failed", async () => {
    post.mockRejectedValue(new Error("offline"));
    useRecordingFeedbackStore
      .getState()
      .show({ recording_id: "br-1", workflow_permanent_id: "wpid-1" });
    renderPrompt();

    await click("Thumbs up");

    expect(screen.getByText("Couldn't save. Try again.")).toBeTruthy();
    expect(feedbackEvents()).toEqual([]);
  });

  it("credits a slow save to the recording that was rated, not a newer one", async () => {
    let finishSave!: () => void;
    post.mockReturnValue(
      new Promise<void>((resolve) => (finishSave = resolve)),
    );
    useRecordingFeedbackStore.getState().show({
      recording_attempt_id: "rra-1",
      recording_id: "br-1",
      workflow_permanent_id: "wpid-1",
    });
    renderPrompt();

    await click("Thumbs up");
    act(() =>
      useRecordingFeedbackStore.getState().show({
        recording_attempt_id: "rra-2",
        recording_id: "br-2",
        workflow_permanent_id: "wpid-1",
      }),
    );
    await act(async () => finishSave());

    expect(feedbackEvents()).toEqual([
      expect.objectContaining({
        recording_attempt_id: "rra-1",
        recording_id: "br-1",
        rating: "up",
      }),
    ]);
    expect(useRecordingFeedbackStore.getState().target).toMatchObject({
      recording_attempt_id: "rra-2",
      rating: null,
      responded: false,
    });
  });
});
