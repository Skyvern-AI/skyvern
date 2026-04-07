import { describe, expect, test } from "vitest";
import type {
  Parameter,
  WorkflowParameter,
} from "@/routes/workflows/types/workflowTypes";
import {
  applyScheduleParameterChange,
  createScheduleParameterState,
  getScheduleParameterValidationResult,
} from "./useScheduleParameterState";

function makeWorkflowParam(
  partial: Partial<WorkflowParameter> & {
    key: string;
    workflow_parameter_type: WorkflowParameter["workflow_parameter_type"];
  },
): WorkflowParameter {
  return {
    parameter_type: "workflow",
    key: partial.key,
    description: null,
    workflow_parameter_type: partial.workflow_parameter_type,
    default_value: partial.default_value ?? null,
    workflow_parameter_id: "wp_test",
    workflow_id: "w_test",
    created_at: "2026-01-01T00:00:00Z",
    modified_at: "2026-01-01T00:00:00Z",
    deleted_at: null,
  } as WorkflowParameter;
}

describe("createScheduleParameterState", () => {
  test("builds values from workflow defaults when no stored values exist", () => {
    const workflowParameters: ReadonlyArray<Parameter> = [
      makeWorkflowParam({
        key: "name",
        workflow_parameter_type: "string",
      }),
      makeWorkflowParam({
        key: "enabled",
        workflow_parameter_type: "boolean",
        default_value: "true",
      }),
    ];

    expect(createScheduleParameterState(workflowParameters, null)).toEqual({
      values: {
        name: "",
        enabled: true,
      },
      errors: {},
    });
  });

  test("prefers stored values over workflow defaults", () => {
    const workflowParameters: ReadonlyArray<Parameter> = [
      makeWorkflowParam({
        key: "payload",
        workflow_parameter_type: "json",
        default_value: { foo: "default" },
      }),
      makeWorkflowParam({
        key: "count",
        workflow_parameter_type: "integer",
        default_value: 1,
      }),
    ];

    expect(
      createScheduleParameterState(workflowParameters, {
        payload: '{"foo":"custom"}',
        count: 5,
      }),
    ).toEqual({
      values: {
        payload: '{"foo":"custom"}',
        count: 5,
      },
      errors: {},
    });
  });
});

describe("applyScheduleParameterChange", () => {
  test("updates the parameter value and clears only that parameter's error", () => {
    const nextState = applyScheduleParameterChange(
      {
        values: {
          source: "",
          untouched: 1,
        },
        errors: {
          source: "Required",
          untouched: "Required",
        },
      },
      "source",
      "https://example.com",
    );

    expect(nextState).toEqual({
      values: {
        source: "https://example.com",
        untouched: 1,
      },
      errors: {
        untouched: "Required",
      },
    });
  });
});

describe("getScheduleParameterValidationResult", () => {
  test("returns the validation errors and invalid status", () => {
    const workflowParameters: ReadonlyArray<Parameter> = [
      makeWorkflowParam({
        key: "credential",
        workflow_parameter_type: "credential_id",
      }),
    ];

    expect(
      getScheduleParameterValidationResult(workflowParameters, {
        credential: "",
      }),
    ).toEqual({
      errors: {
        credential: "Required",
      },
      isValid: false,
    });
  });

  test("returns an empty error map when all required parameters are present", () => {
    const workflowParameters: ReadonlyArray<Parameter> = [
      makeWorkflowParam({
        key: "threshold",
        workflow_parameter_type: "float",
      }),
    ];

    expect(
      getScheduleParameterValidationResult(workflowParameters, {
        threshold: 3.14,
      }),
    ).toEqual({
      errors: {},
      isValid: true,
    });
  });
});
