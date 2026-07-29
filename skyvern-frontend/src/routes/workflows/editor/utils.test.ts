import { describe, expect, test } from "vitest";

import {
  type WorkflowApiResponse,
  WorkflowParameterValueType,
} from "../types/workflowTypes";
import {
  applySkyvernCredentialEdit,
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
            fallback_credential_ids: ["cred_backup"],
            fallback_trigger: "any_failure",
            created_at: "2026-06-23T00:00:00Z",
            modified_at: "2026-06-23T00:00:00Z",
            deleted_at: null,
          },
        ],
      },
    };

    const initialParameters = getInitialParameters(workflow);

    expect(initialParameters).toEqual([
      expect.objectContaining({
        key: "credential_id",
        parameterType: "credential",
        dataType: WorkflowParameterValueType.CredentialId,
      }),
      expect.objectContaining({
        key: "static_credential",
        fallbackCredentialIds: ["cred_backup"],
        fallbackTrigger: "any_failure",
      }),
    ]);
    expect(initialParameters[1]).not.toHaveProperty("dataType");
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
      fallback_credential_ids: null,
      fallback_trigger: null,
      key: "credentials",
      description: null,
    });
  });

  test("serializes a single credential with fallbacks as a credential parameter", () => {
    const parameter: SkyvernCredential = {
      key: "credentials",
      parameterType: "credential",
      credentialId: "cred_1",
      fallbackCredentialIds: ["cred_backup"],
      fallbackTrigger: "credential_failures",
    };

    expect(skyvernCredentialToParameterYAML(parameter)).toEqual({
      parameter_type: "credential",
      credential_id: "cred_1",
      credential_ids: null,
      selection_strategy: null,
      fallback_credential_ids: ["cred_backup"],
      fallback_trigger: "credential_failures",
      key: "credentials",
      description: null,
    });
  });
});

describe("applySkyvernCredentialEdit", () => {
  const previous: SkyvernCredential = {
    key: "portal_credential",
    parameterType: "credential",
    credentialId: "cred_primary",
    fallbackCredentialIds: ["cred_backup_1", "cred_backup_2"],
    fallbackTrigger: "any_failure",
    dataType: WorkflowParameterValueType.CredentialId,
  };

  test("preserves fallback config on an edit that only touches visible fields", () => {
    expect(
      applySkyvernCredentialEdit(previous, {
        key: "portal_credential",
        credentialId: "cred_primary",
        description: "updated description",
      }),
    ).toEqual({
      key: "portal_credential",
      parameterType: "credential",
      credentialId: "cred_primary",
      description: "updated description",
      credentialIds: null,
      selectionStrategy: null,
      fallbackCredentialIds: ["cred_backup_1", "cred_backup_2"],
      fallbackTrigger: "any_failure",
      dataType: WorkflowParameterValueType.CredentialId,
    });
  });

  test("removes the new primary from the fallback list on a primary change", () => {
    const edited = applySkyvernCredentialEdit(previous, {
      key: "portal_credential",
      credentialId: "cred_backup_1",
    });

    expect(edited.credentialId).toBe("cred_backup_1");
    expect(edited.fallbackCredentialIds).toEqual(["cred_backup_2"]);
    expect(edited.fallbackTrigger).toBe("any_failure");
  });

  test("clears the trigger when the primary change empties the fallback list", () => {
    const edited = applySkyvernCredentialEdit(
      { ...previous, fallbackCredentialIds: ["cred_backup_1"] },
      { key: "portal_credential", credentialId: "cred_backup_1" },
    );

    expect(edited.fallbackCredentialIds).toBeNull();
    expect(edited.fallbackTrigger).toBeNull();
  });

  test("preserves a rotation pool, swapping the new primary into its head", () => {
    const edited = applySkyvernCredentialEdit(
      {
        ...previous,
        credentialIds: ["cred_primary", "cred_rotate_1", "cred_rotate_2"],
        selectionStrategy: "round_robin",
      },
      { key: "portal_credential", credentialId: "cred_new" },
    );

    expect(edited.credentialIds).toEqual([
      "cred_new",
      "cred_rotate_1",
      "cred_rotate_2",
    ]);
    expect(edited.selectionStrategy).toBe("round_robin");
  });

  test("collapses a rotation pool that shrinks below two credentials", () => {
    const edited = applySkyvernCredentialEdit(
      {
        ...previous,
        credentialIds: ["cred_primary", "cred_rotate_1"],
        selectionStrategy: "round_robin",
      },
      { key: "portal_credential", credentialId: "cred_rotate_1" },
    );

    expect(edited.credentialIds).toBeNull();
    expect(edited.selectionStrategy).toBeNull();
  });

  test("returns a plain credential parameter when adding (no previous value)", () => {
    expect(
      applySkyvernCredentialEdit(undefined, {
        key: "credentials",
        credentialId: "cred_1",
      }),
    ).toEqual({
      key: "credentials",
      parameterType: "credential",
      credentialId: "cred_1",
      description: null,
    });
  });

  test("returns a plain credential parameter when the previous value is not a Skyvern credential", () => {
    expect(
      applySkyvernCredentialEdit(
        {
          key: "some_input",
          parameterType: "workflow",
          dataType: WorkflowParameterValueType.String,
          defaultValue: "x",
        },
        { key: "some_input", credentialId: "cred_1" },
      ),
    ).toEqual({
      key: "some_input",
      parameterType: "credential",
      credentialId: "cred_1",
      description: null,
    });
  });
});
