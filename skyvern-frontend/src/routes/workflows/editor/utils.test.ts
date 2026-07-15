import { describe, expect, test } from "vitest";

import {
  type WorkflowApiResponse,
  WorkflowParameterValueType,
} from "../types/workflowTypes";
import {
  constructCacheKeyValueFromParameters,
  getInitialParameters,
  skyvernCredentialToParameterYAML,
} from "./utils";
import { SkyvernCredential } from "./types";

const baseWorkflow = {
  workflow_id: "w_test",
  organization_id: "o_test",
  is_saved_task: false,
  is_template: false,
  title: "Test workflow",
  workflow_permanent_id: "wpid_test",
  version: 1,
  description: "Test workflow",
  workflow_definition: {
    version: 2,
    blocks: [],
    parameters: [],
    finally_block_label: null,
    workflow_system_prompt: null,
  },
  proxy_location: null,
  webhook_callback_url: null,
  extra_http_headers: null,
  cdp_connect_headers: null,
  persist_browser_session: false,
  pin_saved_session_ip: false,
  browser_profile_id: null,
  browser_profile_key: null,
  model: null,
  totp_verification_url: null,
  totp_identifier: null,
  max_screenshot_scrolls: null,
  max_elapsed_time_minutes: null,
  status: null,
  created_at: "2026-06-23T00:00:00Z",
  modified_at: "2026-06-23T00:00:00Z",
  deleted_at: null,
  run_with: "agent",
  cache_key: null,
  ai_fallback: true,
  enable_self_healing: false,
  adaptive_caching: null,
  code_version: null,
  run_sequentially: false,
  sequential_key: null,
  folder_id: null,
  import_error: null,
} satisfies WorkflowApiResponse;

describe("getInitialParameters", () => {
  test("marks workflow credential_id parameters without marking static credential parameters", () => {
    const workflow: WorkflowApiResponse = {
      ...baseWorkflow,
      workflow_definition: {
        ...baseWorkflow.workflow_definition,
        parameters: [
          {
            parameter_type: "workflow",
            workflow_id: "w_test",
            workflow_parameter_id: "wp_credential_input",
            key: "credential_id",
            description: null,
            workflow_parameter_type: WorkflowParameterValueType.CredentialId,
            default_value: "cred_default",
            created_at: "2026-06-23T00:00:00Z",
            modified_at: "2026-06-23T00:00:00Z",
            deleted_at: null,
          },
          {
            parameter_type: "credential",
            workflow_id: "w_test",
            credential_parameter_id: "cp_static",
            key: "static_credential",
            description: null,
            credential_id: "cred_static",
            created_at: "2026-06-23T00:00:00Z",
            modified_at: "2026-06-23T00:00:00Z",
            deleted_at: null,
          },
        ],
      },
    };

    expect(getInitialParameters(workflow)).toEqual([
      expect.objectContaining({
        key: "credential_id",
        parameterType: "credential",
        dataType: WorkflowParameterValueType.CredentialId,
      }),
      expect.not.objectContaining({
        key: "static_credential",
        dataType: WorkflowParameterValueType.CredentialId,
      }),
    ]);
  });
});

describe("skyvernCredentialToParameterYAML", () => {
  test("serializes a freshly selected single credential (no dataType) as an editable workflow credential_id parameter", () => {
    const parameter: SkyvernCredential = {
      key: "credentials",
      parameterType: "credential",
      credentialId: "cred_123",
    };

    expect(skyvernCredentialToParameterYAML(parameter)).toEqual({
      parameter_type: "workflow",
      workflow_parameter_type: WorkflowParameterValueType.CredentialId,
      default_value: "cred_123",
      key: "credentials",
      description: null,
    });
  });

  test("serializes a legacy single credential (dataType set) as an editable workflow credential_id parameter", () => {
    const parameter: SkyvernCredential = {
      key: "credentials",
      parameterType: "credential",
      credentialId: "cred_123",
      dataType: WorkflowParameterValueType.CredentialId,
    };

    expect(skyvernCredentialToParameterYAML(parameter)).toEqual({
      parameter_type: "workflow",
      workflow_parameter_type: WorkflowParameterValueType.CredentialId,
      default_value: "cred_123",
      key: "credentials",
      description: null,
    });
  });

  test("serializes a rotation pool (2+ credentials) as a credential parameter", () => {
    const parameter: SkyvernCredential = {
      key: "credentials",
      parameterType: "credential",
      credentialId: "cred_1",
      credentialIds: ["cred_1", "cred_2"],
      selectionStrategy: "round_robin",
    };

    expect(skyvernCredentialToParameterYAML(parameter)).toEqual({
      parameter_type: "credential",
      credential_id: "cred_1",
      credential_ids: ["cred_1", "cred_2"],
      selection_strategy: "round_robin",
      key: "credentials",
      description: null,
    });
  });
});

describe("constructCacheKeyValueFromParameters", () => {
  test("substitutes a single parameter reference", () => {
    expect(
      constructCacheKeyValueFromParameters({
        codeKey: "{{a}}",
        parameters: { a: "x" },
      }),
    ).toBe("x");
  });

  test("substitutes distinct parameters", () => {
    expect(
      constructCacheKeyValueFromParameters({
        codeKey: "{{a}}-{{b}}",
        parameters: { a: "1", b: "2" },
      }),
    ).toBe("1-2");
  });

  test("replaces every occurrence of a repeated parameter", () => {
    // Regression: String.replace only swapped the first "{{a}}", leaving the
    // second, so the trailing "{" guard discarded the whole key.
    expect(
      constructCacheKeyValueFromParameters({
        codeKey: "{{a}}/{{a}}",
        parameters: { a: "x" },
      }),
    ).toBe("x/x");
  });

  test("returns empty string when a placeholder is left unresolved", () => {
    expect(
      constructCacheKeyValueFromParameters({
        codeKey: "{{a}}-{{missing}}",
        parameters: { a: "x" },
      }),
    ).toBe("");
  });

  test("skips null, undefined, and empty-string parameter values", () => {
    expect(
      constructCacheKeyValueFromParameters({
        codeKey: "static-key",
        parameters: { a: null, b: undefined, c: "" },
      }),
    ).toBe("static-key");
  });
});
