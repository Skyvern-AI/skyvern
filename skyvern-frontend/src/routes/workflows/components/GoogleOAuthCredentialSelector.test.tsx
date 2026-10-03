// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { useState } from "react";
import { ReactFlowProvider } from "@xyflow/react";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import {
  clearDeferredEdits,
  deferredEdits,
} from "@/hooks/useDeferredLockedEdit";
import { WorkflowScopeContext } from "@/routes/workflows/editor/WorkflowScopeContext";

import type { GoogleOAuthCredential } from "@/api/types";
import {
  beginSaveTransaction,
  beginCopilotAcceptance,
  finishCopilotAcceptance,
  createYamlCommitOwner,
  finishSaveTransaction,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";

import { GoogleOAuthCredentialSelector } from "./GoogleOAuthCredentialSelector";

const mocks = vi.hoisted(() => ({
  useGoogleOAuthCredentials: vi.fn(),
}));

vi.mock("@/hooks/useGoogleOAuthCredentials", async (importActual) => {
  const actual =
    await importActual<typeof import("@/hooks/useGoogleOAuthCredentials")>();
  return {
    ...actual,
    useGoogleOAuthCredentials: mocks.useGoogleOAuthCredentials,
  };
});

const driveScope = "https://www.googleapis.com/auth/drive";

function googleCredential(id: string): GoogleOAuthCredential {
  return {
    id,
    organization_id: "org_1",
    credential_name: "Primary Drive",
    state: "active",
    scopes_granted: [driveScope],
    email_address: `${id}@gmail.test`,
    created_at: "2026-08-11T00:00:00Z",
    modified_at: "2026-08-11T00:00:00Z",
  };
}

function DeferredCredentialHarness({
  autoFillFirst,
}: {
  autoFillFirst: boolean;
}) {
  const [value, setValue] = useState("");
  return (
    <ReactFlowProvider>
      <WorkflowScopeContext.Provider
        value={{ workflowId: "workflow-test", readOnly: false }}
      >
        <GoogleOAuthCredentialSelector
          nodeId="d1"
          value={value}
          onChange={setValue}
          requiredScopes={[driveScope]}
        />
        {autoFillFirst && (
          <WorkflowBlockInputTextarea
            name="credentialId:google"
            nodeId="d1"
            value={value}
            onChange={setValue}
            hideActions
          />
        )}
        <output data-testid="saved-credential">{value}</output>
      </WorkflowScopeContext.Provider>
    </ReactFlowProvider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  clearDeferredEdits();
  useWorkflowYamlEditorStore.setState(
    useWorkflowYamlEditorStore.getInitialState(),
  );
  mocks.useGoogleOAuthCredentials.mockReturnValue({
    credentials: [googleCredential("goac-connected")],
    isLoading: false,
    isFetching: false,
    error: null,
  });
});

afterEach(() => {
  cleanup();
  clearDeferredEdits();
  vi.useRealTimers();
  useWorkflowYamlEditorStore.setState(
    useWorkflowYamlEditorStore.getInitialState(),
  );
});

describe("GoogleOAuthCredentialSelector", () => {
  test.each([
    ["save", false],
    ["save", true],
    ["copilot", false],
    ["copilot", true],
  ] as const)(
    "preserves a buffered template after %s unlock (auto-fill first: %s)",
    (lock, autoFillFirst) => {
      vi.useFakeTimers();
      mocks.useGoogleOAuthCredentials.mockReturnValue({
        credentials: [],
        isLoading: false,
        isFetching: true,
        error: null,
      });
      const view = render(
        <DeferredCredentialHarness autoFillFirst={autoFillFirst} />,
      );
      if (!autoFillFirst) {
        fireEvent.click(screen.getByRole("combobox"));
        fireEvent.click(screen.getByText("Use template expression"));
      }
      const userValue = "{{ chosen_credential }}";
      fireEvent.change(screen.getByRole("textbox"), {
        target: { value: userValue },
      });
      let releaseLock: () => void;
      act(() => {
        if (lock === "save") {
          const owner = createYamlCommitOwner("workflow-test");
          useWorkflowYamlEditorStore.setState({ editorOwner: owner });
          expect(beginSaveTransaction(owner)).toBe(true);
          releaseLock = () => finishSaveTransaction(owner);
        } else {
          const token = beginCopilotAcceptance();
          expect(token).not.toBeNull();
          releaseLock = () => finishCopilotAcceptance(token!);
        }
      });
      const deferKey = JSON.stringify([
        "workflow-test",
        "d1",
        "credentialId:google",
      ]);
      expect(deferredEdits.get(deferKey)?.value).toBe(userValue);
      mocks.useGoogleOAuthCredentials.mockReturnValue({
        credentials: [googleCredential("goac-connected")],
        isLoading: false,
        isFetching: false,
        error: null,
      });
      view.rerender(
        <DeferredCredentialHarness autoFillFirst={autoFillFirst} />,
      );
      act(() => vi.advanceTimersByTime(300));
      expect(screen.getByTestId("saved-credential").textContent).toBe("");

      act(() => releaseLock());
      act(() => vi.advanceTimersByTime(300));

      expect(screen.getByTestId("saved-credential").textContent).toBe(
        userValue,
      );
      expect(
        (screen.getAllByRole("textbox")[0] as HTMLTextAreaElement).value,
      ).toBe(userValue);
      expect(deferredEdits.has(deferKey)).toBe(false);
    },
  );

  test("auto-fills credentials loaded during a save only after unlocking", () => {
    const onChange = vi.fn();
    const owner = createYamlCommitOwner("workflow-test");
    useWorkflowYamlEditorStore.setState({ editorOwner: owner });
    expect(beginSaveTransaction(owner)).toBe(true);
    mocks.useGoogleOAuthCredentials.mockReturnValue({
      credentials: [],
      isLoading: true,
      isFetching: true,
      error: null,
    });
    const selector = (value = "") => (
      <GoogleOAuthCredentialSelector
        nodeId="d1"
        value={value}
        onChange={onChange}
        requiredScopes={[driveScope]}
      />
    );
    const { rerender } = render(selector());
    mocks.useGoogleOAuthCredentials.mockReturnValue({
      credentials: [googleCredential("goac-connected")],
      isLoading: false,
      isFetching: false,
      error: null,
    });
    rerender(selector());
    expect(onChange).not.toHaveBeenCalled();

    act(() => finishSaveTransaction(owner));
    expect(onChange).toHaveBeenCalledExactlyOnceWith("goac-connected");
    rerender(selector("goac-connected"));
    act(() => {
      expect(beginSaveTransaction(owner)).toBe(true);
    });
    act(() => finishSaveTransaction(owner));
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  test("keeps an optional credential empty when connected accounts exist", () => {
    const onChange = vi.fn();

    render(
      <GoogleOAuthCredentialSelector
        nodeId="d1"
        value=""
        onChange={onChange}
        requiredScopes={[driveScope]}
        optional
      />,
    );

    expect(onChange).not.toHaveBeenCalled();
  });

  test("can clear an optional selected account", () => {
    const onChange = vi.fn();

    render(
      <GoogleOAuthCredentialSelector
        nodeId="d1"
        value="goac-connected"
        onChange={onChange}
        requiredScopes={[driveScope]}
        optional
      />,
    );

    fireEvent.click(screen.getByRole("combobox"));
    fireEvent.click(screen.getByText("No Google account"));

    expect(onChange).toHaveBeenCalledWith("");
  });
});

test("does not resurrect an unblurred template after switching back to the account picker", () => {
  vi.useFakeTimers();
  mocks.useGoogleOAuthCredentials.mockReturnValue({
    credentials: [],
    isLoading: false,
    isFetching: true,
    error: null,
  });
  render(<DeferredCredentialHarness autoFillFirst={false} />);
  fireEvent.click(screen.getByRole("combobox"));
  fireEvent.click(screen.getByText("Use template expression"));
  fireEvent.change(screen.getByRole("textbox"), {
    target: { value: "{{ discarded_credential }}" },
  });
  expect(screen.getByTestId("saved-credential").textContent).toBe("");
  fireEvent.click(screen.getByRole("button", { name: "Use account picker" }));
  fireEvent.click(screen.getByRole("combobox"));
  fireEvent.click(screen.getByText("Use template expression"));
  act(() => vi.advanceTimersByTime(300));
  expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("");
  expect(screen.getByTestId("saved-credential").textContent).toBe("");
});
