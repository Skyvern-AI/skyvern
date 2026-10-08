// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useRecordedBlocksStore } from "@/store/RecordedBlocksStore";
import {
  useRecordingStore,
  type RecordingDraftStep,
} from "@/store/useRecordingStore";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";
import {
  createYamlCommitOwner,
  registerEditorOwner,
  unregisterEditorOwner,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";
import type { useProcessRecordingMutation } from "@/routes/browserSessions/hooks/useProcessRecordingMutation";

import { RecordingPanel } from "./RecordingPanel";

const mutateMock = vi.fn();
const recordingMutation = vi.hoisted(() => ({
  isPending: false,
  onSuccess: undefined as Parameters<
    typeof useProcessRecordingMutation
  >[0]["onSuccess"],
}));
const modalState = vi.hoisted(() => ({
  overrideType: null as string | null,
  defaultTestUrl: null as string | null,
  defaultTotpType: null as string | null,
  heading: null as string | null,
}));

vi.mock("@/routes/browserSessions/hooks/useProcessRecordingMutation", () => ({
  useProcessRecordingMutation: (
    options: Parameters<typeof useProcessRecordingMutation>[0],
  ) => {
    recordingMutation.onSuccess = options.onSuccess;
    return {
      isPending: recordingMutation.isPending,
      isError: false,
      mutate: mutateMock,
    };
  },
}));

vi.mock("@/routes/credentials/CredentialsModal", () => ({
  CredentialsModal: ({
    isOpen,
    onCredentialCreated,
    overrideType,
    defaultTestUrl,
    defaultTotpType,
    heading,
  }: {
    isOpen?: boolean;
    onCredentialCreated?: (id: string, name?: string) => void;
    overrideType?: string;
    defaultTestUrl?: string;
    defaultTotpType?: string;
    heading?: string;
  }) => {
    modalState.overrideType = overrideType ?? null;
    modalState.defaultTestUrl = defaultTestUrl ?? null;
    modalState.defaultTotpType = defaultTotpType ?? null;
    modalState.heading = heading ?? null;
    return isOpen ? (
      <button
        type="button"
        data-testid="mock-create-credential"
        onClick={() => onCredentialCreated?.("new-cred-1")}
      >
        create credential
      </button>
    ) : null;
  },
}));

const initialRecording = useRecordingStore.getState();
const initialPanel = useWorkflowPanelStore.getState();
const initialRecordedBlocks = useRecordedBlocksStore.getState();
const suggestionHosts: HTMLElement[] = [];

function renderWithSuggestionHost() {
  const suggestionHost = document.createElement("div");
  document.body.appendChild(suggestionHost);
  suggestionHosts.push(suggestionHost);
  return {
    suggestionHost,
    ...render(
      <RecordingPanel
        browserSessionId="pbs_123"
        suggestionPortalTarget={suggestionHost}
      />,
    ),
  };
}

function inputDraft(
  id: string,
  opts: {
    credential_kind?: RecordingDraftStep["credential_kind"];
    url?: string;
    title?: string;
  } = {},
): RecordingDraftStep {
  return {
    step_id: id,
    action_kind: "input_text",
    block_type: "action",
    label: id,
    title: opts.title ?? id,
    status: "ready",
    editable_fields: [],
    parameters: [],
    parameter_keys: [],
    url: opts.url ?? "https://example.com/login",
    credential_kind: opts.credential_kind,
  };
}

describe("RecordingPanel", () => {
  beforeEach(() => {
    HTMLElement.prototype.scrollIntoView = vi.fn();
    HTMLElement.prototype.scrollTo = vi.fn();
    mutateMock.mockReset();
    recordingMutation.isPending = false;
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
    registerEditorOwner(createYamlCommitOwner("wpid-1"));
    modalState.overrideType = null;
    modalState.defaultTestUrl = null;
    modalState.defaultTotpType = null;
    modalState.heading = null;
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
    suggestionHosts.splice(0).forEach((host) => host.remove());
  });

  it("does not change another owner's committing flag after an old request settles", () => {
    const owner = useWorkflowYamlEditorStore.getState().editorOwner!;
    recordingMutation.isPending = true;
    const { rerender } = render(<RecordingPanel browserSessionId="pbs-1" />);
    act(() => {
      unregisterEditorOwner(owner);
      registerEditorOwner(createYamlCommitOwner("wpid-2"));
      useRecordingStore.setState({ isCommitting: true, isRecording: true });
    });
    const recording = useRecordingStore.getState();
    recordingMutation.isPending = false;
    rerender(<RecordingPanel browserSessionId="pbs-2" />);
    expect(useRecordingStore.getState()).toBe(recording);
  });

  it.each([true, false])(
    "publishes recording blocks only for a live owner: %s",
    (live) => {
      const owner = useWorkflowYamlEditorStore.getState().editorOwner!;
      render(<RecordingPanel browserSessionId="pbs-1" />);
      act(() => {
        useRecordingStore.setState({ isRecording: true });
        if (!live) {
          unregisterEditorOwner(owner);
          registerEditorOwner(createYamlCommitOwner("wpid-2"));
        }
      });
      act(() =>
        recordingMutation.onSuccess?.(
          { recordingId: "br-1", blocks: [], parameters: [] },
          owner,
        ),
      );
      expect(useRecordedBlocksStore.getState().owner).toBe(live ? owner : null);
      expect(useRecordingStore.getState().isRecording).toBe(!live);
    },
  );

  it("keeps Stop disabled and never calls process_recording while the browser session id has not resolved", () => {
    render(<RecordingPanel browserSessionId={null} />);

    const stopButton = screen.getByRole("button", {
      name: /stop recording/i,
    }) as HTMLButtonElement;
    expect(stopButton.disabled).toBe(true);

    fireEvent.click(stopButton);

    expect(mutateMock).not.toHaveBeenCalled();
  });

  it("enables Stop and automatically processes once the browser session id resolves", () => {
    const { rerender } = render(<RecordingPanel browserSessionId={null} />);

    expect(
      (
        screen.getByRole("button", {
          name: /stop recording/i,
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);

    rerender(<RecordingPanel browserSessionId="pbs_123" />);

    const stopButton = screen.getByRole("button", {
      name: /stop recording/i,
    }) as HTMLButtonElement;
    expect(stopButton.disabled).toBe(false);

    fireEvent.click(stopButton);

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

      fireEvent.click(screen.getByRole("button", { name: /stop recording/i }));

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

  it("processes once when the same controller moves between chat and full-pane during finishing", () => {
    vi.useFakeTimers();
    const portalTarget = document.createElement("div");
    document.body.appendChild(portalTarget);
    try {
      useRecordingStore.setState({ sessionRevision: 1 });
      const { rerender } = render(
        <RecordingPanel browserSessionId="pbs_123" />,
      );

      fireEvent.click(screen.getByRole("button", { name: /stop recording/i }));
      rerender(
        <RecordingPanel
          browserSessionId="pbs_123"
          expanded
          portalTarget={portalTarget}
        />,
      );
      rerender(<RecordingPanel browserSessionId="pbs_123" />);
      vi.advanceTimersByTime(5000);

      expect(mutateMock).toHaveBeenCalledTimes(1);
    } finally {
      portalTarget.remove();
      vi.useRealTimers();
    }
  });

  it("shows a password suggestion in the outer chat host, not the recording action feed", () => {
    useRecordingStore.setState({
      draftSteps: [
        inputDraft("email", { title: "Fill email" }),
        inputDraft("pw", {
          credential_kind: "password",
          title: "Fill password",
        }),
      ],
    });

    const { suggestionHost } = renderWithSuggestionHost();

    expect(screen.getByText("Fill password")).toBeTruthy();
    expect(screen.getByText("Fill email")).toBeTruthy();
    const addPassword = screen.getByRole("button", { name: /add password/i });
    expect(suggestionHost.contains(addPassword)).toBe(true);
    expect(
      screen.getByTestId("recording-action-feed").contains(addPassword),
    ).toBe(false);
    expect(screen.getByRole("button", { name: /^skip$/i })).toBeTruthy();
  });

  it("shows one suggestion per credential on a site and skip hides it", () => {
    useRecordingStore.setState({
      draftSteps: [
        inputDraft("pw-focus", {
          credential_kind: "password",
          title: "Focus password",
        }),
        inputDraft("pw", {
          credential_kind: "password",
          title: "Fill password",
        }),
      ],
    });

    renderWithSuggestionHost();
    expect(
      screen.getAllByRole("button", { name: /add password/i }),
    ).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: /^skip$/i }));

    expect(screen.queryByRole("button", { name: /add password/i })).toBeNull();
    expect(screen.getByText("Fill password")).toBeTruthy();
  });

  it("opens the password credential modal and dismisses the prompt after create", () => {
    useRecordingStore.setState({
      draftSteps: [
        inputDraft("pw", {
          credential_kind: "password",
          title: "Fill password",
          url: "https://example.com/login",
        }),
      ],
      sessionRevision: 1,
    });

    renderWithSuggestionHost();
    fireEvent.click(screen.getByRole("button", { name: /add password/i }));

    expect(modalState.overrideType).toBe("password");
    expect(modalState.heading).toBe("Add Password");
    expect(modalState.defaultTestUrl).toBe("https://example.com/login");
    expect(screen.getByTestId("mock-create-credential")).toBeTruthy();

    fireEvent.click(screen.getByTestId("mock-create-credential"));

    expect(screen.queryByRole("button", { name: /add password/i })).toBeNull();
    expect(screen.queryByTestId("mock-create-credential")).toBeNull();
    // The committed step carries the credential, which is what makes the backend
    // emit a login block instead of a bare action block.
    expect(
      useRecordingStore.getState().getFinalDraftSteps()?.[0]?.credential_id,
    ).toBe("new-cred-1");
  });

  it.each([
    {
      kind: "credit_card" as const,
      button: /add credit card/i,
      overrideType: "credit-card",
      heading: "Add Credit Card",
      defaultTotpType: null,
    },
    {
      kind: "secret" as const,
      button: /add secret/i,
      overrideType: "secret",
      heading: "Add Secret",
      defaultTotpType: null,
    },
    {
      kind: "totp" as const,
      button: /add two-factor authentication/i,
      overrideType: "password",
      heading: "Add Two-Factor Authentication",
      defaultTotpType: "authenticator",
    },
    {
      kind: "magic_link" as const,
      button: /add magic link/i,
      overrideType: "password",
      heading: "Add Magic Link",
      defaultTotpType: "email",
    },
  ])(
    "opens the $heading popup for a $kind draft",
    ({ kind, button, overrideType, heading, defaultTotpType }) => {
      useRecordingStore.setState({
        draftSteps: [
          inputDraft("secret-step", {
            credential_kind: kind,
            title: `Fill ${kind}`,
            url: "https://example.com/pay",
          }),
        ],
      });

      renderWithSuggestionHost();
      fireEvent.click(screen.getByRole("button", { name: button }));

      expect(modalState.overrideType).toBe(overrideType);
      expect(modalState.heading).toBe(heading);
      expect(modalState.defaultTotpType).toBe(defaultTotpType);
      expect(modalState.defaultTestUrl).toBe("https://example.com/pay");
    },
  );
});
