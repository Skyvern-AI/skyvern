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

import type { MicrosoftOAuthCredential } from "@/api/types";
import {
  beginSaveTransaction,
  beginCopilotAcceptance,
  finishCopilotAcceptance,
  createYamlCommitOwner,
  finishSaveTransaction,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";

import { MicrosoftOAuthCredentialSelector } from "./MicrosoftOAuthCredentialSelector";

const mocks = vi.hoisted(() => ({
  useMicrosoftOAuthCredentials: vi.fn(),
}));

vi.mock("@/hooks/useMicrosoftOAuthCredentials", async (importActual) => {
  const actual =
    await importActual<typeof import("@/hooks/useMicrosoftOAuthCredentials")>();
  return {
    ...actual,
    useMicrosoftOAuthCredentials: mocks.useMicrosoftOAuthCredentials,
  };
});

const mailScope = "Mail.Read";

function microsoftCredential(id: string): MicrosoftOAuthCredential {
  return {
    id,
    organization_id: "org_1",
    credential_name: "Mail account",
    state: "active",
    scopes_granted: [mailScope],
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
        <MicrosoftOAuthCredentialSelector
          nodeId="d1"
          value={value}
          onChange={setValue}
          requiredScopes={[mailScope]}
        />
        {autoFillFirst && (
          <WorkflowBlockInputTextarea
            name="credentialId:microsoft"
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
  mocks.useMicrosoftOAuthCredentials.mockReturnValue({
    credentials: [microsoftCredential("moac-connected")],
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

describe("MicrosoftOAuthCredentialSelector", () => {
  test.each([
    ["save", false],
    ["save", true],
    ["copilot", false],
    ["copilot", true],
  ] as const)(
    "preserves a buffered template after %s unlock (auto-fill first: %s)",
    (lock, autoFillFirst) => {
      vi.useFakeTimers();
      mocks.useMicrosoftOAuthCredentials.mockReturnValue({
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
        "credentialId:microsoft",
      ]);
      expect(deferredEdits.get(deferKey)?.value).toBe(userValue);
      mocks.useMicrosoftOAuthCredentials.mockReturnValue({
        credentials: [microsoftCredential("moac-connected")],
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
    mocks.useMicrosoftOAuthCredentials.mockReturnValue({
      credentials: [],
      isLoading: true,
      isFetching: true,
      error: null,
    });
    const selector = (value = "") => (
      <MicrosoftOAuthCredentialSelector
        nodeId="d1"
        value={value}
        onChange={onChange}
        requiredScopes={[mailScope]}
      />
    );
    const { rerender } = render(selector());
    mocks.useMicrosoftOAuthCredentials.mockReturnValue({
      credentials: [microsoftCredential("moac-connected")],
      isLoading: false,
      isFetching: false,
      error: null,
    });
    rerender(selector());
    expect(onChange).not.toHaveBeenCalled();

    act(() => finishSaveTransaction(owner));
    expect(onChange).toHaveBeenCalledExactlyOnceWith("moac-connected");
    rerender(selector("moac-connected"));
    act(() => {
      expect(beginSaveTransaction(owner)).toBe(true);
    });
    act(() => finishSaveTransaction(owner));
    expect(onChange).toHaveBeenCalledTimes(1);
  });
});

test("does not resurrect an unblurred template after switching back to the account picker", () => {
  vi.useFakeTimers();
  mocks.useMicrosoftOAuthCredentials.mockReturnValue({
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
