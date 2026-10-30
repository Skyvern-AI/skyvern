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
const powerBiScope =
  "https://analysis.windows.net/powerbi/api/Dataset.Read.All";

function microsoftCredential(
  id: string,
  scopes: string[] = [mailScope],
  state = "active",
  credentialName = "Mail account",
): MicrosoftOAuthCredential {
  return {
    id,
    organization_id: "org_1",
    credential_name: credentialName,
    state,
    scopes_granted: scopes,
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

  test("only offers active accounts with the selected Microsoft profile", () => {
    mocks.useMicrosoftOAuthCredentials.mockReturnValue({
      credentials: [
        microsoftCredential(
          "moac_powerbi",
          [powerBiScope],
          "active",
          "Power BI account",
        ),
        microsoftCredential(
          "moac_outlook",
          [mailScope],
          "active",
          "Outlook account",
        ),
        microsoftCredential(
          "moac_expired",
          [powerBiScope],
          "error",
          "Expired Power BI account",
        ),
      ],
      isLoading: false,
      isFetching: false,
      error: null,
    });
    render(
      <ReactFlowProvider>
        <WorkflowScopeContext.Provider
          value={{ workflowId: "workflow-test", readOnly: false }}
        >
          <MicrosoftOAuthCredentialSelector
            nodeId="powerbi"
            value=""
            onChange={vi.fn()}
            requiredScopes={["Dataset.Read.All"]}
            integrationName="Power BI"
          />
        </WorkflowScopeContext.Provider>
      </ReactFlowProvider>,
    );

    fireEvent.click(screen.getByRole("combobox"));
    expect(screen.getByText("Power BI account")).toBeTruthy();
    expect(screen.queryByText("Outlook account")).toBeNull();
    expect(screen.queryByText("Expired Power BI account")).toBeNull();
  });

  test("keeps template mode for Email Inbox but not Power BI", () => {
    const renderSelector = (integrationName?: string) =>
      render(
        <ReactFlowProvider>
          <WorkflowScopeContext.Provider
            value={{ workflowId: "workflow-test", readOnly: false }}
          >
            <MicrosoftOAuthCredentialSelector
              nodeId="selector"
              value=""
              onChange={vi.fn()}
              requiredScopes={[mailScope]}
              integrationName={integrationName}
            />
          </WorkflowScopeContext.Provider>
        </ReactFlowProvider>,
      );

    const emailInbox = renderSelector();
    fireEvent.click(screen.getByRole("combobox"));
    expect(screen.getByText("Use template expression")).toBeTruthy();
    emailInbox.unmount();

    renderSelector("Power BI");
    fireEvent.click(screen.getByRole("combobox"));
    expect(screen.queryByText("Use template expression")).toBeNull();
  });

  test("shows an empty-state message for Email Inbox when no accounts are available", () => {
    mocks.useMicrosoftOAuthCredentials.mockReturnValue({
      credentials: [],
      isLoading: false,
      isFetching: false,
      error: null,
    });

    render(<DeferredCredentialHarness autoFillFirst={false} />);

    expect(
      screen.getByText(/No active Microsoft connections are available/),
    ).toBeTruthy();
  });

  test("disables the Email Inbox picker and shows a retry message on load error", () => {
    mocks.useMicrosoftOAuthCredentials.mockReturnValue({
      credentials: [],
      isLoading: false,
      isFetching: false,
      error: new Error("offline"),
    });

    render(<DeferredCredentialHarness autoFillFirst={false} />);

    expect(screen.getByRole("alert").textContent).toContain(
      "Could not load Microsoft connections",
    );
    expect(screen.getByRole("combobox").hasAttribute("disabled")).toBe(true);
  });

  test.each([
    { description: "non-editable block", editable: false, readOnly: false },
    {
      description: "workflow comparison scope",
      editable: true,
      readOnly: true,
    },
  ])(
    "does not auto-fill or enable Email Inbox in a $description",
    ({ editable, readOnly }) => {
      const onChange = vi.fn();
      render(
        <ReactFlowProvider>
          <WorkflowScopeContext.Provider
            value={{ workflowId: "workflow-test", readOnly }}
          >
            <MicrosoftOAuthCredentialSelector
              nodeId="email-inbox"
              value=""
              onChange={onChange}
              requiredScopes={[mailScope]}
              editable={editable}
            />
          </WorkflowScopeContext.Provider>
        </ReactFlowProvider>,
      );

      expect(screen.getByRole("combobox").hasAttribute("disabled")).toBe(true);
      expect(onChange).not.toHaveBeenCalled();
    },
  );

  test("shows a saved inactive connection as requiring reconnection", () => {
    mocks.useMicrosoftOAuthCredentials.mockReturnValue({
      credentials: [
        microsoftCredential(
          "moac_expired",
          [powerBiScope],
          "error",
          "Expired Power BI account",
        ),
      ],
      isLoading: false,
      isFetching: false,
      error: null,
    });
    render(
      <ReactFlowProvider>
        <WorkflowScopeContext.Provider
          value={{ workflowId: "workflow-test", readOnly: false }}
        >
          <MicrosoftOAuthCredentialSelector
            nodeId="powerbi"
            value="moac_expired"
            onChange={vi.fn()}
            requiredScopes={["Dataset.Read.All"]}
            integrationName="Power BI"
          />
        </WorkflowScopeContext.Provider>
      </ReactFlowProvider>,
    );

    expect(screen.getByText(/needs to be reconnected/).textContent).toContain(
      "needs to be reconnected",
    );
    fireEvent.click(screen.getByRole("combobox"));
    expect(screen.queryByText("Expired Power BI account")).toBeNull();
  });

  test("does not auto-fill or enable connection controls in a read-only editor", () => {
    const onChange = vi.fn();
    mocks.useMicrosoftOAuthCredentials.mockReturnValue({
      credentials: [microsoftCredential("moac_powerbi", [powerBiScope])],
      isLoading: false,
      isFetching: false,
      error: null,
    });
    render(
      <ReactFlowProvider>
        <WorkflowScopeContext.Provider
          value={{ workflowId: "workflow-test", readOnly: false }}
        >
          <MicrosoftOAuthCredentialSelector
            nodeId="powerbi"
            value=""
            onChange={onChange}
            requiredScopes={["Dataset.Read.All"]}
            editable={false}
            integrationName="Power BI"
          />
        </WorkflowScopeContext.Provider>
      </ReactFlowProvider>,
    );

    expect(screen.getByRole("combobox").hasAttribute("disabled")).toBe(true);
    expect(onChange).not.toHaveBeenCalled();
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
