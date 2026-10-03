import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
} from "@testing-library/react";
import { type ChangeEvent, createElement, useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { CredentialApiResponse } from "@/api/types";
import {
  clearDeferredEdits,
  useDeferredLockedEdit,
} from "@/hooks/useDeferredLockedEdit";
import { useWorkflowYamlEditorStore } from "@/store/WorkflowYamlEditorStore";

import {
  useWorkflowScopeId,
  WorkflowScopeContext,
} from "../../WorkflowScopeContext";
import { loginNodeDefaultData } from "./types";
import { useLoginGoalAutoFill } from "./useLoginGoalAutoFill";

const DEFAULT_GOAL = loginNodeDefaultData.navigationGoal;
const NODE_ID = "login-node";

const CRED_WITH_CONTEXT: CredentialApiResponse = {
  credential_id: "cred-a",
  credential_type: "password",
  credential: { username: "alice", totp_type: "none" },
  name: "Cred A",
  user_context: "Click the SSO button first",
};

const CRED_NO_CONTEXT: CredentialApiResponse = {
  credential_id: "cred-b",
  credential_type: "password",
  credential: { username: "bob", totp_type: "none" },
  name: "Cred B",
  user_context: null,
};

function setup(props: {
  selectedCredentialId: string | undefined;
  credentials: Array<CredentialApiResponse>;
  currentGoal: string;
  editable?: boolean;
}) {
  const onAutoFill = vi.fn();
  const rendered = renderHook(
    (p: typeof props) =>
      useLoginGoalAutoFill({
        nodeId: NODE_ID,
        editable: p.editable ?? true,
        selectedCredentialId: p.selectedCredentialId,
        credentials: p.credentials,
        currentGoal: p.currentGoal,
        onAutoFill,
      }),
    { initialProps: props },
  );
  return { ...rendered, onAutoFill };
}

type GoalProps = {
  goal: string;
  onChange: (value: string) => void;
};

function DeferredGoalInput({ goal, onChange }: GoalProps) {
  const workflowId = useWorkflowScopeId();
  const input = useDeferredLockedEdit({
    value: goal,
    onChange,
    deferKey: JSON.stringify([workflowId, NODE_ID, "navigationGoal"]),
  });
  return createElement("textarea", {
    value: input.value,
    disabled: input.mutationLocked,
    onChange: (event: ChangeEvent<HTMLTextAreaElement>) =>
      input.onChange(event.target.value),
  });
}

function GoalAutoFill({
  goal,
  onChange,
  credentials,
}: GoalProps & { credentials: Array<CredentialApiResponse> }) {
  useLoginGoalAutoFill({
    nodeId: NODE_ID,
    editable: true,
    selectedCredentialId: "cred-a",
    credentials,
    currentGoal: goal,
    onAutoFill: onChange,
  });
  return null;
}

function LoginGoalHarness({
  credentials,
  autoFillFirst,
  onAutoFill,
}: {
  credentials: Array<CredentialApiResponse>;
  autoFillFirst: boolean;
  onAutoFill?: (value: string) => void;
}) {
  const [goal, setGoal] = useState(DEFAULT_GOAL);
  const input = createElement(DeferredGoalInput, {
    key: "input",
    goal,
    onChange: setGoal,
  });
  const autoFill = createElement(GoalAutoFill, {
    key: "auto-fill",
    goal,
    onChange: (value) => {
      onAutoFill?.(value);
      setGoal(value);
    },
    credentials,
  });
  return createElement(
    WorkflowScopeContext.Provider,
    { value: { workflowId: "workflow-a", readOnly: false } },
    ...(autoFillFirst ? [autoFill, input] : [input, autoFill]),
    createElement("output", { "data-testid": "saved-goal" }, goal),
  );
}

describe("useLoginGoalAutoFill", () => {
  afterEach(() => {
    cleanup();
    clearDeferredEdits();
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
      true,
    );
    vi.useRealTimers();
  });

  it.each([false, true])(
    "preserves a deferred goal when credentials resolve under lock (auto-fill first: %s)",
    (autoFillFirst) => {
      vi.useFakeTimers();
      const onAutoFill = vi.fn();
      const { rerender } = render(
        createElement(LoginGoalHarness, {
          credentials: [],
          autoFillFirst,
          onAutoFill,
        }),
      );
      const userGoal = "Use the account picker before entering the password";
      fireEvent.change(screen.getByRole("textbox"), {
        target: { value: userGoal },
      });
      act(() => {
        useWorkflowYamlEditorStore.setState({
          copilotAcceptance: Symbol("turn"),
        });
      });
      rerender(
        createElement(LoginGoalHarness, {
          credentials: [CRED_WITH_CONTEXT],
          autoFillFirst,
          onAutoFill,
        }),
      );
      act(() => vi.advanceTimersByTime(300));
      expect(screen.getByTestId("saved-goal").textContent).toBe(DEFAULT_GOAL);

      act(() => {
        useWorkflowYamlEditorStore.setState({ copilotAcceptance: null });
      });
      act(() => vi.advanceTimersByTime(300));

      expect(screen.getByTestId("saved-goal").textContent).toBe(userGoal);
      expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe(
        userGoal,
      );
      expect(onAutoFill).not.toHaveBeenCalled();
    },
  );

  it("fills an untouched goal when credentials resolve under lock", () => {
    const { rerender } = render(
      createElement(LoginGoalHarness, {
        credentials: [],
        autoFillFirst: false,
      }),
    );
    act(() => {
      useWorkflowYamlEditorStore.setState({
        copilotAcceptance: Symbol("turn"),
      });
    });
    rerender(
      createElement(LoginGoalHarness, {
        credentials: [CRED_WITH_CONTEXT],
        autoFillFirst: false,
      }),
    );
    expect(screen.getByTestId("saved-goal").textContent).toBe(DEFAULT_GOAL);

    act(() => {
      useWorkflowYamlEditorStore.setState({ copilotAcceptance: null });
    });

    expect(screen.getByTestId("saved-goal").textContent).toContain(
      CRED_WITH_CONTEXT.user_context,
    );
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe(
      screen.getByTestId("saved-goal").textContent,
    );
  });

  it("does not fire before the credentials list resolves", () => {
    const { onAutoFill } = setup({
      selectedCredentialId: "cred-a",
      credentials: [],
      currentGoal: DEFAULT_GOAL,
    });
    expect(onAutoFill).not.toHaveBeenCalled();
  });

  it("fires once the credentials list resolves after mount — the workflow-open path", () => {
    const { rerender, onAutoFill } = setup({
      selectedCredentialId: "cred-a",
      credentials: [],
      currentGoal: DEFAULT_GOAL,
    });
    expect(onAutoFill).not.toHaveBeenCalled();

    rerender({
      selectedCredentialId: "cred-a",
      credentials: [CRED_WITH_CONTEXT],
      currentGoal: DEFAULT_GOAL,
    });

    expect(onAutoFill).toHaveBeenCalledTimes(1);
    expect(onAutoFill.mock.calls[0]![0]).toContain(
      "Click the SSO button first",
    );
  });

  it.each(["save", "yaml"] as const)(
    "defers credentials resolved during a %s until the lock releases, then fills once",
    (lock) => {
      const { rerender, onAutoFill } = setup({
        selectedCredentialId: "cred-a",
        credentials: [],
        currentGoal: DEFAULT_GOAL,
      });

      act(() => {
        useWorkflowYamlEditorStore.setState({
          commitInProgress: true,
          lockKind: lock,
        });
      });
      rerender({
        selectedCredentialId: "cred-a",
        credentials: [CRED_WITH_CONTEXT],
        currentGoal: DEFAULT_GOAL,
      });
      expect(onAutoFill).not.toHaveBeenCalled();

      act(() => {
        useWorkflowYamlEditorStore.setState({
          commitInProgress: false,
          lockKind: null,
        });
      });
      expect(onAutoFill).toHaveBeenCalledTimes(1);
      expect(onAutoFill).toHaveBeenCalledWith(
        expect.stringContaining(CRED_WITH_CONTEXT.user_context!),
      );

      rerender({
        selectedCredentialId: "cred-a",
        credentials: [CRED_WITH_CONTEXT],
        currentGoal: onAutoFill.mock.calls[0]![0] as string,
      });
      expect(onAutoFill).toHaveBeenCalledTimes(1);
    },
  );

  it("never clobbers a user-authored goal once the credential resolves", () => {
    const { rerender, onAutoFill } = setup({
      selectedCredentialId: "cred-a",
      credentials: [],
      currentGoal: "My own bespoke login steps",
    });

    rerender({
      selectedCredentialId: "cred-a",
      credentials: [CRED_WITH_CONTEXT],
      currentGoal: "My own bespoke login steps",
    });

    expect(onAutoFill).not.toHaveBeenCalled();
  });

  it("does not re-fire on an unrelated re-render (e.g. the user typing)", () => {
    const { rerender, onAutoFill } = setup({
      selectedCredentialId: "cred-a",
      credentials: [CRED_WITH_CONTEXT],
      currentGoal: DEFAULT_GOAL,
    });
    expect(onAutoFill).toHaveBeenCalledTimes(1);

    rerender({
      selectedCredentialId: "cred-a",
      credentials: [CRED_WITH_CONTEXT],
      currentGoal: "the user is now typing something else entirely",
    });

    expect(onAutoFill).toHaveBeenCalledTimes(1);
  });

  it("restores the plain default when the resolved credential switches to one with no instructions", () => {
    const { rerender, onAutoFill } = setup({
      selectedCredentialId: "cred-a",
      credentials: [CRED_WITH_CONTEXT],
      currentGoal: DEFAULT_GOAL,
    });
    expect(onAutoFill).toHaveBeenCalledTimes(1);
    const filled = onAutoFill.mock.calls[0]![0] as string;

    rerender({
      selectedCredentialId: "cred-b",
      credentials: [CRED_WITH_CONTEXT, CRED_NO_CONTEXT],
      currentGoal: filled,
    });

    expect(onAutoFill).toHaveBeenCalledTimes(2);
    expect(onAutoFill.mock.calls[1]![0]).toBe(DEFAULT_GOAL);
  });

  it("does nothing while not editable", () => {
    const { rerender, onAutoFill } = setup({
      selectedCredentialId: "cred-a",
      credentials: [],
      currentGoal: DEFAULT_GOAL,
      editable: false,
    });

    rerender({
      selectedCredentialId: "cred-a",
      credentials: [CRED_WITH_CONTEXT],
      currentGoal: DEFAULT_GOAL,
      editable: false,
    });

    expect(onAutoFill).not.toHaveBeenCalled();
  });
});
