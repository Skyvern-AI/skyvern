import { describe, expect, test } from "vitest";

import {
  CredentialParameter,
  WorkflowApiResponse,
  WorkflowBlock,
  WorkflowParameter,
  WorkflowParameterTypes,
  WorkflowParameterValueType,
} from "./types/workflowTypes";
import {
  getFallbackCredentialIds,
  getLoginCredentialInputs,
  getRotatingCredentialIds,
} from "./runWorkflowCredentials";

function workflowParameter(key: string): WorkflowParameter {
  return {
    parameter_type: WorkflowParameterTypes.Workflow,
    workflow_id: "w_1",
    workflow_parameter_id: `wp_${key}`,
    workflow_parameter_type: WorkflowParameterValueType.CredentialId,
    default_value: `cred_${key}`,
    key,
    description: null,
    created_at: "2026-07-08T00:00:00Z",
    modified_at: "2026-07-08T00:00:00Z",
    deleted_at: null,
  };
}

function rotatingCredentialParameter(
  key: string,
  credentialIds: Array<string> = ["cred_1", "cred_2"],
): CredentialParameter {
  return {
    parameter_type: WorkflowParameterTypes.Credential,
    workflow_id: "w_1",
    credential_parameter_id: `cp_${key}`,
    credential_id: credentialIds[0]!,
    credential_ids: credentialIds,
    selection_strategy: "round_robin",
    key,
    description: null,
    created_at: "2026-07-08T00:00:00Z",
    modified_at: "2026-07-08T00:00:00Z",
    deleted_at: null,
  };
}

function workflowWithBlocks(
  blocks: Array<WorkflowBlock>,
  parameters: WorkflowApiResponse["workflow_definition"]["parameters"] = [],
): WorkflowApiResponse {
  return {
    workflow_definition: {
      blocks,
      parameters,
    },
  } as unknown as WorkflowApiResponse;
}

function loginBlock(label: string, parameters: Array<unknown>): WorkflowBlock {
  return {
    block_type: "login",
    label,
    parameters,
  } as WorkflowBlock;
}

describe("getLoginCredentialInputs", () => {
  test("returns credential_id workflow parameters referenced by login blocks", () => {
    const loginCredential = workflowParameter("login_credential");
    const unrelatedCredential = workflowParameter("api_credential");

    const inputs = getLoginCredentialInputs({
      workflow: workflowWithBlocks([
        loginBlock("Portal login", [loginCredential]),
      ]),
      workflowParameters: [loginCredential, unrelatedCredential],
    });

    expect(inputs).toEqual([
      {
        parameter: loginCredential,
        loginBlockLabels: ["Portal login"],
        fallbackCredentialIds: [],
        fallbackTrigger: null,
      },
    ]);
  });

  test("deduplicates shared credential parameters across nested login blocks", () => {
    const credential = workflowParameter("account_credential");

    const inputs = getLoginCredentialInputs({
      workflow: workflowWithBlocks([
        loginBlock("Primary login", [credential]),
        {
          block_type: "for_loop",
          label: "Loop accounts",
          loop_blocks: [loginBlock("Nested login", [credential])],
        } as WorkflowBlock,
      ]),
      workflowParameters: [credential],
    });

    expect(inputs).toEqual([
      {
        parameter: credential,
        loginBlockLabels: ["Primary login", "Nested login"],
        fallbackCredentialIds: [],
        fallbackTrigger: null,
      },
    ]);
  });

  test("returns block-scoped credential rotation parameters", () => {
    const credential = rotatingCredentialParameter("account_credential");

    const inputs = getLoginCredentialInputs({
      workflow: workflowWithBlocks(
        [loginBlock("Rotating login", [credential])],
        [credential],
      ),
      workflowParameters: [],
    });

    expect(inputs).toEqual([
      {
        parameter: credential,
        loginBlockLabels: ["Rotating login"],
        fallbackCredentialIds: [],
        fallbackTrigger: null,
      },
    ]);
  });

  test("returns single-credential parameters that have fallback credentials", () => {
    const credential: CredentialParameter = {
      ...rotatingCredentialParameter("account_credential", ["cred_1"]),
      credential_ids: null,
      selection_strategy: null,
      fallback_credential_ids: ["cred_fb1"],
    };

    const inputs = getLoginCredentialInputs({
      workflow: workflowWithBlocks(
        [loginBlock("Fallback login", [credential])],
        [credential],
      ),
      workflowParameters: [],
    });

    expect(inputs).toEqual([
      {
        parameter: credential,
        loginBlockLabels: ["Fallback login"],
        fallbackCredentialIds: ["cred_fb1"],
        fallbackTrigger: "credential_failures",
      },
    ]);
    expect((inputs[0]!.parameter as CredentialParameter).credential_id).toBe(
      "cred_1",
    );
  });

  test("passes ordered fallback credential ids through and defaults the trigger", () => {
    const credential: CredentialParameter = {
      ...rotatingCredentialParameter("account_credential"),
      fallback_credential_ids: ["cred_fb2", "cred_fb1", "cred_fb2"],
    };

    const inputs = getLoginCredentialInputs({
      workflow: workflowWithBlocks(
        [loginBlock("Rotating login", [credential])],
        [credential],
      ),
      workflowParameters: [],
    });

    expect(inputs[0]!.fallbackCredentialIds).toEqual(["cred_fb2", "cred_fb1"]);
    expect(inputs[0]!.fallbackTrigger).toBe("credential_failures");
  });

  test("preserves an explicit any_failure fallback trigger", () => {
    const credential: CredentialParameter = {
      ...rotatingCredentialParameter("account_credential"),
      fallback_credential_ids: ["cred_fb1"],
      fallback_trigger: "any_failure",
    };

    const inputs = getLoginCredentialInputs({
      workflow: workflowWithBlocks(
        [loginBlock("Rotating login", [credential])],
        [credential],
      ),
      workflowParameters: [],
    });

    expect(inputs[0]!.fallbackTrigger).toBe("any_failure");
  });

  test("classifies fallback-only parameters as single-credential, not rotating", () => {
    const credential: CredentialParameter = {
      ...rotatingCredentialParameter("account_credential", ["cred_1"]),
      credential_ids: null,
      selection_strategy: null,
      fallback_credential_ids: ["cred_fb1", "cred_fb2"],
    };

    expect(getRotatingCredentialIds(credential)).toEqual([]);
    expect(getFallbackCredentialIds(credential)).toEqual([
      "cred_fb1",
      "cred_fb2",
    ]);
  });

  test("ignores block-scoped single credentials", () => {
    const credential = rotatingCredentialParameter("account_credential", [
      "cred_1",
    ]);

    const inputs = getLoginCredentialInputs({
      workflow: workflowWithBlocks([
        loginBlock("Fixed login", [
          {
            parameter_type: WorkflowParameterTypes.Credential,
            key: credential.key,
            credential_id: "cred_1",
            credential_ids: null,
            selection_strategy: null,
          },
        ]),
      ]),
      workflowParameters: [],
    });

    expect(inputs).toEqual([]);
  });
});

describe("getFallbackCredentialIds", () => {
  test("deduplicates fallback credential ids while preserving order", () => {
    const credential = {
      ...rotatingCredentialParameter("account_credential"),
      fallback_credential_ids: ["fallback_1", "fallback_2", "fallback_1"],
    };

    expect(getFallbackCredentialIds(credential)).toEqual([
      "fallback_1",
      "fallback_2",
    ]);
  });

  test("ignores empty and invalid fallback credential ids", () => {
    const credential = {
      ...rotatingCredentialParameter("account_credential"),
      fallback_credential_ids: [
        "fallback_1",
        "",
        null,
        "fallback_2",
      ] as unknown as Array<string>,
    };

    expect(getFallbackCredentialIds(credential)).toEqual([
      "fallback_1",
      "fallback_2",
    ]);
  });

  test("returns an empty list when fallback credentials are unset", () => {
    const credential = rotatingCredentialParameter("account_credential");

    expect(getFallbackCredentialIds(credential)).toEqual([]);
  });
});
