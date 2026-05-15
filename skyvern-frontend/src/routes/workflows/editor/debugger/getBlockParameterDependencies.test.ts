import { describe, expect, it } from "vitest";

import { getBlockParameterDependencies } from "./getBlockParameterDependencies";
import {
  WorkflowParameterValueType,
  type WorkflowBlock,
  type WorkflowParameter,
} from "@/routes/workflows/types/workflowTypes";

function workflowParam(
  key: string,
  workflowParameterType: WorkflowParameter["workflow_parameter_type"] = "string",
): WorkflowParameter {
  return {
    parameter_type: "workflow",
    key,
    description: null,
    workflow_id: "wf-test",
    workflow_parameter_id: `wpid-${key}`,
    workflow_parameter_type: workflowParameterType,
    default_value: null,
    created_at: "",
    modified_at: "",
    deleted_at: null,
  };
}

describe("getBlockParameterDependencies", () => {
  it("returns all parameters when block is undefined", () => {
    const params = [workflowParam("a"), workflowParam("b")];
    expect(getBlockParameterDependencies(undefined, params)).toEqual(params);
  });

  it("returns all parameters when workflow has no parameters", () => {
    const block = { block_type: "navigation", label: "n" } as WorkflowBlock;
    expect(getBlockParameterDependencies(block, [])).toEqual([]);
  });

  it("collects keys from parameter_keys", () => {
    const params = [workflowParam("x"), workflowParam("y")];
    const block = {
      block_type: "navigation",
      label: "n",
      parameter_keys: ["y"],
      url: null,
      title: "",
      navigation_goal: null,
      error_code_mapping: null,
      max_retries: 0,
      max_steps_per_run: null,
      complete_on_download: false,
      download_suffix: null,
      totp_verification_url: null,
      totp_identifier: null,
      disable_cache: false,
      complete_criterion: null,
      terminate_criterion: null,
      engine: null,
      include_action_history_in_verification: false,
    } as unknown as WorkflowBlock;

    expect(getBlockParameterDependencies(block, params)).toEqual([params[1]]);
  });

  it("collects keys from jinja in string fields", () => {
    const params = [workflowParam("portal_url"), workflowParam("other")];
    const block = {
      block_type: "navigation",
      label: "n",
      parameter_keys: [],
      url: "{{ portal_url }}/login",
      title: "",
      navigation_goal: null,
      error_code_mapping: null,
      max_retries: 0,
      max_steps_per_run: null,
      complete_on_download: false,
      download_suffix: null,
      totp_verification_url: null,
      totp_identifier: null,
      disable_cache: false,
      complete_criterion: null,
      terminate_criterion: null,
      engine: null,
      include_action_history_in_verification: false,
    } as unknown as WorkflowBlock;

    expect(getBlockParameterDependencies(block, params)).toEqual([params[0]]);
  });

  it("parses jinja with filters", () => {
    const params = [workflowParam("x")];
    const block = {
      block_type: "goto_url",
      label: "u",
      url: "{{ x | default('') }}",
    } as unknown as WorkflowBlock;

    expect(getBlockParameterDependencies(block, params)).toEqual([params[0]]);
  });

  it("collects workflow params referenced inside jinja expressions", () => {
    const params = [workflowParam("count"), workflowParam("limit")];
    const block = {
      block_type: "conditional",
      label: "branch",
      branch_conditions: [
        {
          id: "1",
          criteria: {
            criteria_type: "jinja2_template",
            expression: "{{ count < limit }}",
          },
          next_block_label: null,
          description: null,
          is_default: false,
        },
      ],
    } as unknown as WorkflowBlock;

    const result = getBlockParameterDependencies(block, params);
    expect(result.map((p) => p.key).sort()).toEqual(["count", "limit"]);
  });

  it("collects workflow params from subscript jinja", () => {
    const params = [workflowParam("items"), workflowParam("other")];
    const block = {
      block_type: "goto_url",
      label: "u",
      url: "{{ items[0] }}",
    } as unknown as WorkflowBlock;

    expect(getBlockParameterDependencies(block, params)).toEqual([params[0]]);
  });

  it("resolves context parameters to their source workflow parameter", () => {
    const params = [workflowParam("source_key"), workflowParam("other")];
    const block = {
      block_type: "navigation",
      label: "n",
      parameter_keys: [],
      parameters: [
        {
          parameter_type: "context",
          key: "ctx_alias",
          description: null,
          source: workflowParam("source_key"),
          value: null,
        },
      ],
      url: null,
      title: "",
      navigation_goal: null,
      error_code_mapping: null,
      max_retries: 0,
      max_steps_per_run: null,
      complete_on_download: false,
      download_suffix: null,
      totp_verification_url: null,
      totp_identifier: null,
      disable_cache: false,
      complete_criterion: null,
      terminate_criterion: null,
      engine: null,
      include_action_history_in_verification: false,
    } as unknown as WorkflowBlock;

    expect(getBlockParameterDependencies(block, params)).toEqual([params[0]]);
  });

  it("returns empty when nothing on the block matches workflow parameters", () => {
    const params = [workflowParam("only_in_workflow")];
    const block = {
      block_type: "goto_url",
      label: "u",
      url: "https://example.com",
    } as unknown as WorkflowBlock;

    expect(getBlockParameterDependencies(block, params)).toEqual([]);
  });

  it("only includes credential params attached to the login block", () => {
    const params = [
      workflowParam("u", WorkflowParameterValueType.String),
      workflowParam("c1", WorkflowParameterValueType.CredentialId),
      workflowParam("c2", WorkflowParameterValueType.CredentialId),
    ];
    const block = {
      block_type: "login",
      label: "login",
      parameter_keys: ["u"],
      parameters: [workflowParam("c1")],
      url: null,
      title: "",
      navigation_goal: null,
      error_code_mapping: null,
      max_retries: 0,
      max_steps_per_run: null,
      totp_verification_url: null,
      totp_identifier: null,
      disable_cache: false,
      complete_criterion: null,
      terminate_criterion: null,
      engine: null,
    } as unknown as WorkflowBlock;

    const result = getBlockParameterDependencies(block, params);
    expect(result.map((p) => p.key).sort()).toEqual(["c1", "u"]);
  });

  it("includes loop_over key for for_loop blocks", () => {
    const params = [workflowParam("items"), workflowParam("other")];
    const block = {
      block_type: "for_loop",
      label: "loop",
      loop_over: workflowParam("items"),
      loop_blocks: [],
      loop_variable_reference: null,
      complete_if_empty: false,
    } as unknown as WorkflowBlock;

    expect(getBlockParameterDependencies(block, params)).toEqual([params[0]]);
  });
});
