import { renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { CredentialApiResponse } from "@/api/types";

import { loginNodeDefaultData } from "./types";
import { useLoginGoalAutoFill } from "./useLoginGoalAutoFill";

const DEFAULT_GOAL = loginNodeDefaultData.navigationGoal;

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

describe("useLoginGoalAutoFill", () => {
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
