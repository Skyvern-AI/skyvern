import { describe, expect, test } from "vitest";

import {
  type WorkflowApiResponse,
  WorkflowParameterValueType,
} from "../types/workflowTypes";
import {
  constructCacheKeyValueFromParameters,
  getInitialParameters,
} from "./utils";

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

describe("constructCacheKeyValueFromParameters", () => {
  test("replaces every occurrence of a repeated parameter placeholder", () => {
    expect(
      constructCacheKeyValueFromParameters({
        codeKey: "{{a}}/{{a}}",
        parameters: { a: "x" },
      }),
    ).toBe("x/x");
  });
});
