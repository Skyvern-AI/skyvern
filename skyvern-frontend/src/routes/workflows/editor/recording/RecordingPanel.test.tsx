// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useRecordedBlocksStore } from "@/store/RecordedBlocksStore";
import {
  useRecordingStore,
  type RecordingDraftStep,
} from "@/store/useRecordingStore";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";

import { RecordingPanel } from "./RecordingPanel";

const mutateMock = vi.fn();
const modalState = vi.hoisted(() => ({
  overrideType: null as string | null,
  defaultTestUrl: null as string | null,
  defaultTotpType: null as string | null,
  heading: null as string | null,
}));

vi.mock("@/routes/browserSessions/hooks/useProcessRecordingMutation", () => ({
  useProcessRecordingMutation: () => ({
    isPending: false,
    isError: false,
    mutate: mutateMock,
  }),
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
    mutateMock.mockReset();
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

  it("shows add and dismiss on a password draft, not on an email draft", () => {
    useRecordingStore.setState({
      draftSteps: [
        inputDraft("email", { title: "Fill email" }),
        inputDraft("pw", {
          credential_kind: "password",
          title: "Fill password",
        }),
      ],
    });

    render(<RecordingPanel browserSessionId="pbs_123" />);

    expect(screen.getByText("Fill password")).toBeTruthy();
    expect(screen.getByText("Fill email")).toBeTruthy();
    expect(screen.getByRole("button", { name: /add password/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /^dismiss$/i })).toBeTruthy();
  });

  it("hides the credential prompt after dismiss", () => {
    useRecordingStore.setState({
      draftSteps: [
        inputDraft("pw", {
          credential_kind: "password",
          title: "Fill password",
        }),
      ],
    });

    render(<RecordingPanel browserSessionId="pbs_123" />);
    fireEvent.click(screen.getByRole("button", { name: /^dismiss$/i }));

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

    render(<RecordingPanel browserSessionId="pbs_123" />);
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

      render(<RecordingPanel browserSessionId="pbs_123" />);
      fireEvent.click(screen.getByRole("button", { name: button }));

      expect(modalState.overrideType).toBe(overrideType);
      expect(modalState.heading).toBe(heading);
      expect(modalState.defaultTotpType).toBe(defaultTotpType);
      expect(modalState.defaultTestUrl).toBe("https://example.com/pay");
    },
  );
});
