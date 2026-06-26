import { describe, expect, it } from "vitest";

import type { CredentialApiResponse } from "@/api/types";

import {
  decideDebugLoginPlayGate,
  evaluateDebugSessionProfileCompatibility,
  gateActionFromBackendCompatibility,
  resolveLoginBlockCredential,
} from "../debugSessionProfileCompatibility";

const LOGIN_BLOCK_WITH_CRED = {
  block_type: "login",
  parameters: [{ parameter_type: "credential", credential_id: "cred_42" }],
};

const LOGIN_BLOCK_WITH_WORKFLOW_PARAM_CRED = {
  block_type: "login",
  parameters: [
    {
      parameter_type: "workflow",
      key: "credential_param",
      workflow_parameter_type: "credential_id",
      default_value: "cred_42",
    },
  ],
};

const LOGIN_BLOCK_WITH_WORKFLOW_PARAM_CRED_NO_DEFAULT = {
  block_type: "login",
  parameters: [
    {
      parameter_type: "workflow",
      key: "credential_param",
      workflow_parameter_type: "credential_id",
      default_value: null,
    },
  ],
};

const TASK_BLOCK = {
  block_type: "task",
  parameters: [],
};

function makeCredential(
  overrides: Partial<CredentialApiResponse>,
): CredentialApiResponse {
  return {
    credential_id: "cred_default",
    credential: { username: "u", totp_type: "none" },
    credential_type: "password",
    name: "default",
    browser_profile_id: null,
    ...overrides,
  };
}

describe("evaluateDebugSessionProfileCompatibility", () => {
  it("returns compatible when the credential has no saved browser profile", () => {
    const result = evaluateDebugSessionProfileCompatibility({
      pbsBrowserProfileId: null,
      credentialBrowserProfileId: null,
    });

    expect(result).toEqual({ compatible: true, reason: null });
  });

  it("returns pbs_no_profile when the PBS has no profile but the credential does", () => {
    const result = evaluateDebugSessionProfileCompatibility({
      pbsBrowserProfileId: null,
      credentialBrowserProfileId: "bp_cred",
    });

    expect(result).toEqual({ compatible: false, reason: "pbs_no_profile" });
  });

  it("returns pbs_different_profile when the saved profiles differ", () => {
    const result = evaluateDebugSessionProfileCompatibility({
      pbsBrowserProfileId: "bp_other",
      credentialBrowserProfileId: "bp_cred",
    });

    expect(result).toEqual({
      compatible: false,
      reason: "pbs_different_profile",
    });
  });

  it("returns compatible when the PBS profile matches the credential profile", () => {
    const result = evaluateDebugSessionProfileCompatibility({
      pbsBrowserProfileId: "bp_same",
      credentialBrowserProfileId: "bp_same",
    });

    expect(result).toEqual({ compatible: true, reason: null });
  });
});

describe("resolveLoginBlockCredential", () => {
  it("returns no-credential-parameter when the block is not a LoginBlock", () => {
    const result = resolveLoginBlockCredential({
      block: { block_type: "task", parameters: [] },
      credentials: [],
    });
    expect(result).toEqual({ status: "no-credential-parameter" });
  });

  it("returns no-credential-parameter when the LoginBlock has no credential param", () => {
    const result = resolveLoginBlockCredential({
      block: {
        block_type: "login",
        parameters: [{ parameter_type: "workflow", key: "x" }],
      },
      credentials: [],
    });
    expect(result).toEqual({ status: "no-credential-parameter" });
  });

  it("returns credential-not-in-list when the referenced credential is missing from the supplied list", () => {
    const result = resolveLoginBlockCredential({
      block: {
        block_type: "login",
        parameters: [
          { parameter_type: "credential", credential_id: "cred_missing" },
        ],
      },
      credentials: [makeCredential({ credential_id: "cred_other" })],
    });
    expect(result).toEqual({
      status: "credential-not-in-list",
      credentialId: "cred_missing",
    });
  });

  it("returns credential-not-in-list when credentials list is empty but the block references one", () => {
    const result = resolveLoginBlockCredential({
      block: {
        block_type: "login",
        parameters: [
          { parameter_type: "credential", credential_id: "cred_anything" },
        ],
      },
      credentials: [],
    });
    expect(result).toEqual({
      status: "credential-not-in-list",
      credentialId: "cred_anything",
    });
  });

  it("returns resolved with the credential's saved browser_profile_id when present", () => {
    const result = resolveLoginBlockCredential({
      block: {
        block_type: "login",
        parameters: [
          { parameter_type: "credential", credential_id: "cred_42" },
        ],
      },
      credentials: [
        makeCredential({
          credential_id: "cred_42",
          browser_profile_id: "bp_42",
        }),
      ],
    });
    expect(result).toEqual({ status: "resolved", browserProfileId: "bp_42" });
  });

  it("returns resolved with null profile when the credential exists but has no profile", () => {
    const result = resolveLoginBlockCredential({
      block: {
        block_type: "login",
        parameters: [
          { parameter_type: "credential", credential_id: "cred_42" },
        ],
      },
      credentials: [
        makeCredential({ credential_id: "cred_42", browser_profile_id: null }),
      ],
    });
    expect(result).toEqual({ status: "resolved", browserProfileId: null });
  });

  it("resolves a workflow-parameter CREDENTIAL_ID via its default_value when the credential is in the list", () => {
    const result = resolveLoginBlockCredential({
      block: LOGIN_BLOCK_WITH_WORKFLOW_PARAM_CRED,
      credentials: [
        makeCredential({
          credential_id: "cred_42",
          browser_profile_id: "bp_42",
        }),
      ],
    });
    expect(result).toEqual({ status: "resolved", browserProfileId: "bp_42" });
  });

  it("returns credential-not-in-list when a workflow-parameter CREDENTIAL_ID default_value is missing from the list", () => {
    const result = resolveLoginBlockCredential({
      block: LOGIN_BLOCK_WITH_WORKFLOW_PARAM_CRED,
      credentials: [makeCredential({ credential_id: "cred_other" })],
    });
    expect(result).toEqual({
      status: "credential-not-in-list",
      credentialId: "cred_42",
    });
  });

  it("returns credential-not-in-list with an empty id when a workflow-parameter CREDENTIAL_ID has no default_value (fail-closed to backend lookup)", () => {
    const result = resolveLoginBlockCredential({
      block: LOGIN_BLOCK_WITH_WORKFLOW_PARAM_CRED_NO_DEFAULT,
      credentials: [
        makeCredential({
          credential_id: "cred_42",
          browser_profile_id: "bp_42",
        }),
      ],
    });
    expect(result).toEqual({
      status: "credential-not-in-list",
      credentialId: "",
    });
  });

  it("ignores a workflow parameter that is not of type credential_id (existing direct behavior unchanged)", () => {
    const result = resolveLoginBlockCredential({
      block: {
        block_type: "login",
        parameters: [
          {
            parameter_type: "workflow",
            key: "some_text",
            workflow_parameter_type: "string",
            default_value: "hello",
          },
        ],
      },
      credentials: [],
    });
    expect(result).toEqual({ status: "no-credential-parameter" });
  });
});

describe("decideDebugLoginPlayGate", () => {
  it("proceeds when the block isn't a LoginBlock", () => {
    const action = decideDebugLoginPlayGate({
      blockType: "task",
      hasDebugSession: true,
      credentialsState: "ready",
      block: TASK_BLOCK,
      credentials: [],
      pbsBrowserProfileId: null,
    });
    expect(action).toEqual({ kind: "proceed" });
  });

  it("proceeds when there is no debug session", () => {
    const action = decideDebugLoginPlayGate({
      blockType: "login",
      hasDebugSession: false,
      credentialsState: "ready",
      block: LOGIN_BLOCK_WITH_CRED,
      credentials: [],
      pbsBrowserProfileId: null,
    });
    expect(action).toEqual({ kind: "proceed" });
  });

  it("returns block-loading while credentials are still loading", () => {
    const action = decideDebugLoginPlayGate({
      blockType: "login",
      hasDebugSession: true,
      credentialsState: "loading",
      block: LOGIN_BLOCK_WITH_CRED,
      credentials: undefined,
      pbsBrowserProfileId: null,
    });
    expect(action).toEqual({ kind: "block-loading" });
  });

  it("returns block-retry with credentials-error when the credentials query errored", () => {
    const action = decideDebugLoginPlayGate({
      blockType: "login",
      hasDebugSession: true,
      credentialsState: "error",
      block: LOGIN_BLOCK_WITH_CRED,
      credentials: undefined,
      pbsBrowserProfileId: null,
    });
    expect(action).toEqual({
      kind: "block-retry",
      reason: "credentials-error",
    });
  });

  it("returns block-loading defensively when state is ready but data is undefined", () => {
    const action = decideDebugLoginPlayGate({
      blockType: "login",
      hasDebugSession: true,
      credentialsState: "ready",
      block: LOGIN_BLOCK_WITH_CRED,
      credentials: undefined,
      pbsBrowserProfileId: null,
    });
    expect(action).toEqual({ kind: "block-loading" });
  });

  it("returns block-retry with credential-not-found when the referenced credential is missing from the fetched list", () => {
    const action = decideDebugLoginPlayGate({
      blockType: "login",
      hasDebugSession: true,
      credentialsState: "ready",
      block: LOGIN_BLOCK_WITH_CRED,
      credentials: [
        // cred_42 is what the block references; only cred_other is in the
        // fetched window (e.g. paginated past page 1).
        makeCredential({
          credential_id: "cred_other",
          browser_profile_id: "bp_other",
        }),
      ],
      pbsBrowserProfileId: "bp_other",
    });
    expect(action).toEqual({
      kind: "block-retry",
      reason: "credential-not-found",
    });
  });

  it("returns block-retry with credential-not-found when the fetched list is empty", () => {
    const action = decideDebugLoginPlayGate({
      blockType: "login",
      hasDebugSession: true,
      credentialsState: "ready",
      block: LOGIN_BLOCK_WITH_CRED,
      credentials: [],
      pbsBrowserProfileId: "bp_anything",
    });
    expect(action).toEqual({
      kind: "block-retry",
      reason: "credential-not-found",
    });
  });

  it("shows the modal when ready credentials reveal a profile mismatch", () => {
    const action = decideDebugLoginPlayGate({
      blockType: "login",
      hasDebugSession: true,
      credentialsState: "ready",
      block: LOGIN_BLOCK_WITH_CRED,
      credentials: [
        makeCredential({
          credential_id: "cred_42",
          browser_profile_id: "bp_42",
        }),
      ],
      pbsBrowserProfileId: "bp_other",
    });
    expect(action).toEqual({
      kind: "show-modal",
      reason: "pbs_different_profile",
    });
  });

  it("shows the modal when the PBS lacks a profile but the credential has one", () => {
    const action = decideDebugLoginPlayGate({
      blockType: "login",
      hasDebugSession: true,
      credentialsState: "ready",
      block: LOGIN_BLOCK_WITH_CRED,
      credentials: [
        makeCredential({
          credential_id: "cred_42",
          browser_profile_id: "bp_42",
        }),
      ],
      pbsBrowserProfileId: null,
    });
    expect(action).toEqual({ kind: "show-modal", reason: "pbs_no_profile" });
  });

  it("proceeds when credentials are ready and profiles match", () => {
    const action = decideDebugLoginPlayGate({
      blockType: "login",
      hasDebugSession: true,
      credentialsState: "ready",
      block: LOGIN_BLOCK_WITH_CRED,
      credentials: [
        makeCredential({
          credential_id: "cred_42",
          browser_profile_id: "bp_same",
        }),
      ],
      pbsBrowserProfileId: "bp_same",
    });
    expect(action).toEqual({ kind: "proceed" });
  });

  it("proceeds when the LoginBlock credential has no saved profile, regardless of PBS state", () => {
    const action = decideDebugLoginPlayGate({
      blockType: "login",
      hasDebugSession: true,
      credentialsState: "ready",
      block: LOGIN_BLOCK_WITH_CRED,
      credentials: [
        makeCredential({ credential_id: "cred_42", browser_profile_id: null }),
      ],
      pbsBrowserProfileId: "bp_anything",
    });
    expect(action).toEqual({ kind: "proceed" });
  });

  it("proceeds when the LoginBlock has no credential parameter at all", () => {
    const action = decideDebugLoginPlayGate({
      blockType: "login",
      hasDebugSession: true,
      credentialsState: "ready",
      block: { block_type: "login", parameters: [] },
      credentials: [],
      pbsBrowserProfileId: "bp_anything",
    });
    expect(action).toEqual({ kind: "proceed" });
  });

  it("shows the modal for a workflow-parameter CREDENTIAL_ID whose default_value resolves to a different profile", () => {
    const action = decideDebugLoginPlayGate({
      blockType: "login",
      hasDebugSession: true,
      credentialsState: "ready",
      block: LOGIN_BLOCK_WITH_WORKFLOW_PARAM_CRED,
      credentials: [
        makeCredential({
          credential_id: "cred_42",
          browser_profile_id: "bp_42",
        }),
      ],
      pbsBrowserProfileId: "bp_other",
    });
    expect(action).toEqual({
      kind: "show-modal",
      reason: "pbs_different_profile",
    });
  });

  it("proceeds when a workflow-parameter CREDENTIAL_ID resolves to a matching profile", () => {
    const action = decideDebugLoginPlayGate({
      blockType: "login",
      hasDebugSession: true,
      credentialsState: "ready",
      block: LOGIN_BLOCK_WITH_WORKFLOW_PARAM_CRED,
      credentials: [
        makeCredential({
          credential_id: "cred_42",
          browser_profile_id: "bp_same",
        }),
      ],
      pbsBrowserProfileId: "bp_same",
    });
    expect(action).toEqual({ kind: "proceed" });
  });

  it("returns block-retry with credential-not-found when a workflow-parameter CREDENTIAL_ID default_value is not in the bounded list", () => {
    const action = decideDebugLoginPlayGate({
      blockType: "login",
      hasDebugSession: true,
      credentialsState: "ready",
      block: LOGIN_BLOCK_WITH_WORKFLOW_PARAM_CRED,
      credentials: [
        makeCredential({
          credential_id: "cred_other",
          browser_profile_id: "bp_other",
        }),
      ],
      pbsBrowserProfileId: "bp_other",
    });
    expect(action).toEqual({
      kind: "block-retry",
      reason: "credential-not-found",
    });
  });

  it("returns block-retry with credential-not-found when a workflow-parameter CREDENTIAL_ID has no default_value (fail-closed)", () => {
    const action = decideDebugLoginPlayGate({
      blockType: "login",
      hasDebugSession: true,
      credentialsState: "ready",
      block: LOGIN_BLOCK_WITH_WORKFLOW_PARAM_CRED_NO_DEFAULT,
      credentials: [
        makeCredential({
          credential_id: "cred_42",
          browser_profile_id: "bp_42",
        }),
      ],
      pbsBrowserProfileId: "bp_other",
    });
    expect(action).toEqual({
      kind: "block-retry",
      reason: "credential-not-found",
    });
  });
});

describe("gateActionFromBackendCompatibility", () => {
  it("returns proceed when the backend reports compatible", () => {
    expect(
      gateActionFromBackendCompatibility({ compatible: true, reason: null }),
    ).toEqual({ kind: "proceed" });
  });

  it("fail-safes to block-retry credentials-error when the backend payload is incompatible-but-reasonless", () => {
    expect(
      gateActionFromBackendCompatibility({ compatible: false, reason: null }),
    ).toEqual({ kind: "block-retry", reason: "credentials-error" });
  });

  it("returns show-modal with pbs_no_profile when the backend says so", () => {
    expect(
      gateActionFromBackendCompatibility({
        compatible: false,
        reason: "pbs_no_profile",
      }),
    ).toEqual({ kind: "show-modal", reason: "pbs_no_profile" });
  });

  it("returns show-modal with pbs_different_profile when the backend says so", () => {
    expect(
      gateActionFromBackendCompatibility({
        compatible: false,
        reason: "pbs_different_profile",
      }),
    ).toEqual({ kind: "show-modal", reason: "pbs_different_profile" });
  });
});
