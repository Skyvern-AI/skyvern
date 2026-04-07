import { describe, test, expect } from "vitest";
import {
  isScheduleParameter,
  isRequired,
  isMissingRequiredValue,
  hasUserFacingParameters,
  buildInitialParameterValues,
  buildScheduleParametersPayload,
  formatScheduleParameterValue,
  validateScheduleParameters,
} from "./scheduleParameters";
import type {
  Parameter,
  WorkflowParameter,
} from "@/routes/workflows/types/workflowTypes";

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
    // Cast-only fields the helpers never read:
    workflow_parameter_id: "wp_test",
    workflow_id: "w_test",
    created_at: "2026-01-01T00:00:00Z",
    modified_at: "2026-01-01T00:00:00Z",
    deleted_at: null,
  } as WorkflowParameter;
}

describe("isScheduleParameter", () => {
  test("returns true for workflow parameters", () => {
    const param = makeWorkflowParam({
      key: "x",
      workflow_parameter_type: "string",
    });
    expect(isScheduleParameter(param)).toBe(true);
  });

  test("returns false for context parameters", () => {
    const param = { parameter_type: "context", key: "ctx" } as Parameter;
    expect(isScheduleParameter(param)).toBe(false);
  });

  test("returns false for output parameters", () => {
    const param = { parameter_type: "output", key: "out" } as Parameter;
    expect(isScheduleParameter(param)).toBe(false);
  });

  test("returns false for aws_secret parameters", () => {
    const param = { parameter_type: "aws_secret", key: "s" } as Parameter;
    expect(isScheduleParameter(param)).toBe(false);
  });
});

describe("isRequired", () => {
  test("returns true for null default_value", () => {
    const p = makeWorkflowParam({
      key: "k",
      workflow_parameter_type: "string",
      default_value: null,
    });
    expect(isRequired(p)).toBe(true);
  });

  test("returns true for undefined default_value", () => {
    const p = makeWorkflowParam({
      key: "k",
      workflow_parameter_type: "string",
    });
    // makeWorkflowParam coalesces undefined → null via ??, so explicitly set:
    (p as { default_value: unknown }).default_value = undefined;
    expect(isRequired(p)).toBe(true);
  });

  test("returns false for empty string default", () => {
    const p = makeWorkflowParam({
      key: "k",
      workflow_parameter_type: "string",
      default_value: "",
    });
    expect(isRequired(p)).toBe(false);
  });

  test("returns false for false boolean default", () => {
    const p = makeWorkflowParam({
      key: "k",
      workflow_parameter_type: "boolean",
      default_value: false,
    });
    expect(isRequired(p)).toBe(false);
  });

  test("returns false for 0 numeric default", () => {
    const p = makeWorkflowParam({
      key: "k",
      workflow_parameter_type: "integer",
      default_value: 0,
    });
    expect(isRequired(p)).toBe(false);
  });

  test("returns false for non-empty string default", () => {
    const p = makeWorkflowParam({
      key: "k",
      workflow_parameter_type: "string",
      default_value: "hello",
    });
    expect(isRequired(p)).toBe(false);
  });
});

describe("isMissingRequiredValue", () => {
  test("null is missing for every type", () => {
    const types = [
      "string",
      "integer",
      "float",
      "boolean",
      "json",
      "file_url",
      "credential_id",
    ] as const;
    for (const t of types) {
      const p = makeWorkflowParam({ key: "k", workflow_parameter_type: t });
      expect(isMissingRequiredValue(p, null)).toBe(true);
    }
  });

  test("undefined is missing for every type", () => {
    const p = makeWorkflowParam({
      key: "k",
      workflow_parameter_type: "string",
    });
    expect(isMissingRequiredValue(p, undefined)).toBe(true);
  });

  test("empty string is NOT missing for string type", () => {
    const p = makeWorkflowParam({
      key: "k",
      workflow_parameter_type: "string",
    });
    expect(isMissingRequiredValue(p, "")).toBe(false);
  });

  test("empty string IS missing for integer/float/boolean/json/file_url/credential_id", () => {
    const types = [
      "integer",
      "float",
      "boolean",
      "json",
      "file_url",
      "credential_id",
    ] as const;
    for (const t of types) {
      const p = makeWorkflowParam({ key: "k", workflow_parameter_type: t });
      expect(isMissingRequiredValue(p, "")).toBe(true);
    }
  });

  test("whitespace-only string is missing for json", () => {
    const p = makeWorkflowParam({ key: "k", workflow_parameter_type: "json" });
    expect(isMissingRequiredValue(p, "   ")).toBe(true);
  });

  test("whitespace-only string is missing for credential_id", () => {
    const p = makeWorkflowParam({
      key: "k",
      workflow_parameter_type: "credential_id",
    });
    expect(isMissingRequiredValue(p, "   ")).toBe(true);
  });

  test("whitespace-only string is missing for integer/float/boolean", () => {
    const types = ["integer", "float", "boolean"] as const;
    for (const t of types) {
      const p = makeWorkflowParam({ key: "k", workflow_parameter_type: t });
      expect(isMissingRequiredValue(p, "   ")).toBe(true);
    }
  });

  test("whitespace-only string is missing for file_url", () => {
    const p = makeWorkflowParam({
      key: "k",
      workflow_parameter_type: "file_url",
    });
    expect(isMissingRequiredValue(p, "   ")).toBe(true);
  });

  test("empty dict is missing for file_url", () => {
    const p = makeWorkflowParam({
      key: "k",
      workflow_parameter_type: "file_url",
    });
    expect(isMissingRequiredValue(p, {})).toBe(true);
  });

  test("file_url dict with empty s3uri is missing", () => {
    const p = makeWorkflowParam({
      key: "k",
      workflow_parameter_type: "file_url",
    });
    expect(isMissingRequiredValue(p, { s3uri: "" })).toBe(true);
  });

  test("file_url dict with non-empty s3uri is not missing", () => {
    const p = makeWorkflowParam({
      key: "k",
      workflow_parameter_type: "file_url",
    });
    expect(isMissingRequiredValue(p, { s3uri: "s3://bucket/k" })).toBe(false);
  });

  test("valid values for each type are not missing", () => {
    const cases: Array<
      [WorkflowParameter["workflow_parameter_type"], unknown]
    > = [
      ["string", "hello"],
      ["integer", 42],
      ["float", 3.14],
      ["boolean", true],
      ["boolean", false],
      ["json", '{"k": 1}'],
      ["credential_id", "cred_abc"],
    ];
    for (const [t, v] of cases) {
      const p = makeWorkflowParam({ key: "k", workflow_parameter_type: t });
      expect(isMissingRequiredValue(p, v)).toBe(false);
    }
  });
});

describe("hasUserFacingParameters", () => {
  test("returns false for empty array", () => {
    expect(hasUserFacingParameters([])).toBe(false);
  });

  test("returns false when only context/output parameters exist", () => {
    const params = [
      { parameter_type: "context", key: "c" } as Parameter,
      { parameter_type: "output", key: "o" } as Parameter,
    ];
    expect(hasUserFacingParameters(params)).toBe(false);
  });

  test("returns true when at least one workflow parameter exists", () => {
    const params = [
      { parameter_type: "context", key: "c" } as Parameter,
      makeWorkflowParam({ key: "w", workflow_parameter_type: "string" }),
    ];
    expect(hasUserFacingParameters(params)).toBe(true);
  });
});

describe("buildInitialParameterValues", () => {
  test("returns empty object when no workflow parameters", () => {
    expect(buildInitialParameterValues([], null)).toEqual({});
    expect(buildInitialParameterValues([], {})).toEqual({});
  });

  test("seeds missing keys with parameter default_value", () => {
    const params = [
      makeWorkflowParam({
        key: "a",
        workflow_parameter_type: "string",
        default_value: "hello",
      }),
      makeWorkflowParam({
        key: "b",
        workflow_parameter_type: "integer",
        default_value: 7,
      }),
    ];
    expect(buildInitialParameterValues(params, null)).toEqual({
      a: "hello",
      b: 7,
    });
  });

  test("existing values in stored override defaults", () => {
    const params = [
      makeWorkflowParam({
        key: "a",
        workflow_parameter_type: "string",
        default_value: "hello",
      }),
    ];
    expect(buildInitialParameterValues(params, { a: "custom" })).toEqual({
      a: "custom",
    });
  });

  test("missing stored values with no default yield empty string for string type", () => {
    const params = [
      makeWorkflowParam({
        key: "a",
        workflow_parameter_type: "string",
        default_value: null,
      }),
    ];
    expect(buildInitialParameterValues(params, null)).toEqual({ a: "" });
  });

  test("missing stored values with no default yield null for non-string types", () => {
    const params = [
      makeWorkflowParam({
        key: "n",
        workflow_parameter_type: "integer",
        default_value: null,
      }),
      makeWorkflowParam({
        key: "b",
        workflow_parameter_type: "boolean",
        default_value: null,
      }),
    ];
    expect(buildInitialParameterValues(params, null)).toEqual({
      n: null,
      b: null,
    });
  });

  test("ignores unknown keys in stored (does not pass them through)", () => {
    const params = [
      makeWorkflowParam({
        key: "a",
        workflow_parameter_type: "string",
        default_value: null,
      }),
    ];
    expect(buildInitialParameterValues(params, { a: "x", bogus: "y" })).toEqual(
      { a: "x" },
    );
  });

  test("json parameter with object default is stringified", () => {
    const params = [
      makeWorkflowParam({
        key: "j",
        workflow_parameter_type: "json",
        default_value: { foo: 1, bar: [2, 3] },
      }),
    ];
    const result = buildInitialParameterValues(params, null);
    expect(typeof result.j).toBe("string");
    expect(JSON.parse(result.j as string)).toEqual({ foo: 1, bar: [2, 3] });
  });

  test("json parameter with string default is kept as-is", () => {
    const params = [
      makeWorkflowParam({
        key: "j",
        workflow_parameter_type: "json",
        default_value: '{"foo": 1}',
      }),
    ];
    expect(buildInitialParameterValues(params, null)).toEqual({
      j: '{"foo": 1}',
    });
  });

  test('boolean parameter with string "true" default is coerced to true', () => {
    const params = [
      makeWorkflowParam({
        key: "b",
        workflow_parameter_type: "boolean",
        default_value: "true",
      }),
    ];
    expect(buildInitialParameterValues(params, null)).toEqual({ b: true });
  });

  test('boolean parameter with string "false" default is coerced to false', () => {
    const params = [
      makeWorkflowParam({
        key: "b",
        workflow_parameter_type: "boolean",
        default_value: "false",
      }),
    ];
    expect(buildInitialParameterValues(params, null)).toEqual({ b: false });
  });

  test("boolean parameter with actual true default is passed through", () => {
    const params = [
      makeWorkflowParam({
        key: "b",
        workflow_parameter_type: "boolean",
        default_value: true,
      }),
    ];
    expect(buildInitialParameterValues(params, null)).toEqual({ b: true });
  });

  test("stored value overrides default without coercion", () => {
    // Even if default_value would be stringified, an existing stored value
    // (from an already-saved schedule) must be passed through verbatim so
    // opening+saving the edit form is a no-op.
    const params = [
      makeWorkflowParam({
        key: "j",
        workflow_parameter_type: "json",
        default_value: { foo: 1 },
      }),
    ];
    // Stored value is already a string — passed through.
    expect(buildInitialParameterValues(params, { j: '{"bar": 2}' })).toEqual({
      j: '{"bar": 2}',
    });
  });
});

describe("formatScheduleParameterValue", () => {
  test("null and undefined render as empty string", () => {
    expect(formatScheduleParameterValue(null)).toBe("");
    expect(formatScheduleParameterValue(undefined)).toBe("");
  });

  test("primitive strings render as-is", () => {
    expect(formatScheduleParameterValue("hello")).toBe("hello");
    expect(formatScheduleParameterValue("")).toBe("");
  });

  test("primitive numbers and booleans use String()", () => {
    expect(formatScheduleParameterValue(42)).toBe("42");
    expect(formatScheduleParameterValue(3.14)).toBe("3.14");
    expect(formatScheduleParameterValue(true)).toBe("true");
    expect(formatScheduleParameterValue(false)).toBe("false");
  });

  test("file_url dict with string s3uri unwraps to the URI", () => {
    expect(formatScheduleParameterValue({ s3uri: "s3://bucket/key" })).toBe(
      "s3://bucket/key",
    );
  });

  test("file_url dict with non-string s3uri falls back to JSON", () => {
    const value = { s3uri: null };
    expect(formatScheduleParameterValue(value)).toBe(JSON.stringify(value));
  });

  test("plain objects render as JSON (not [object Object])", () => {
    expect(formatScheduleParameterValue({ foo: 1, bar: [2, 3] })).toBe(
      '{"foo":1,"bar":[2,3]}',
    );
  });

  test("arrays render as JSON", () => {
    expect(formatScheduleParameterValue([1, 2, 3])).toBe("[1,2,3]");
  });
});

describe("validateScheduleParameters", () => {
  test("returns empty object when all required values are present", () => {
    const params = [
      makeWorkflowParam({ key: "a", workflow_parameter_type: "string" }),
      makeWorkflowParam({ key: "b", workflow_parameter_type: "integer" }),
    ];
    expect(validateScheduleParameters(params, { a: "hello", b: 7 })).toEqual(
      {},
    );
  });

  test("flags a single missing required parameter", () => {
    const params = [
      makeWorkflowParam({ key: "a", workflow_parameter_type: "integer" }),
    ];
    expect(validateScheduleParameters(params, {})).toEqual({ a: "Required" });
  });

  test("flags multiple missing required parameters", () => {
    const params = [
      makeWorkflowParam({ key: "a", workflow_parameter_type: "integer" }),
      makeWorkflowParam({ key: "b", workflow_parameter_type: "boolean" }),
    ];
    expect(validateScheduleParameters(params, {})).toEqual({
      a: "Required",
      b: "Required",
    });
  });

  test("ignores parameters that have a default value", () => {
    const params = [
      makeWorkflowParam({
        key: "a",
        workflow_parameter_type: "integer",
        default_value: 42,
      }),
    ];
    expect(validateScheduleParameters(params, {})).toEqual({});
  });

  test("ignores non-workflow parameters (context, output, aws_secret)", () => {
    const params: Parameter[] = [
      { parameter_type: "context", key: "ctx" } as Parameter,
      { parameter_type: "output", key: "out" } as Parameter,
      { parameter_type: "aws_secret", key: "sec" } as Parameter,
    ];
    expect(validateScheduleParameters(params, {})).toEqual({});
  });

  test("flags whitespace-only string for json type (matches backend)", () => {
    const params = [
      makeWorkflowParam({ key: "j", workflow_parameter_type: "json" }),
    ];
    expect(validateScheduleParameters(params, { j: "   " })).toEqual({
      j: "Required",
    });
  });

  test("does NOT flag empty string for plain string type", () => {
    const params = [
      makeWorkflowParam({ key: "s", workflow_parameter_type: "string" }),
    ];
    expect(validateScheduleParameters(params, { s: "" })).toEqual({});
  });
});

describe("buildScheduleParametersPayload", () => {
  test("returns null when there are no parameters", () => {
    expect(buildScheduleParametersPayload({}, [])).toBeNull();
  });

  test("always includes required parameters even when empty string", () => {
    const params = [
      makeWorkflowParam({ key: "name", workflow_parameter_type: "string" }),
    ];
    expect(buildScheduleParametersPayload({ name: "" }, params)).toEqual({
      name: "",
    });
  });

  test("required parameters with values are included as-is", () => {
    const params = [
      makeWorkflowParam({ key: "name", workflow_parameter_type: "string" }),
      makeWorkflowParam({ key: "n", workflow_parameter_type: "integer" }),
    ];
    expect(
      buildScheduleParametersPayload({ name: "alice", n: 42 }, params),
    ).toEqual({ name: "alice", n: 42 });
  });

  test("omits optional parameters whose value still matches the seeded default", () => {
    const params = [
      makeWorkflowParam({
        key: "greeting",
        workflow_parameter_type: "string",
        default_value: "hello",
      }),
      makeWorkflowParam({
        key: "n",
        workflow_parameter_type: "integer",
        default_value: 5,
      }),
    ];
    expect(
      buildScheduleParametersPayload({ greeting: "hello", n: 5 }, params),
    ).toBeNull();
  });

  test("includes optional parameter when user changed it from the default", () => {
    const params = [
      makeWorkflowParam({
        key: "greeting",
        workflow_parameter_type: "string",
        default_value: "hello",
      }),
    ];
    expect(buildScheduleParametersPayload({ greeting: "hi" }, params)).toEqual({
      greeting: "hi",
    });
  });

  test("optional string cleared to empty is dropped (will use default at exec)", () => {
    const params = [
      makeWorkflowParam({
        key: "greeting",
        workflow_parameter_type: "string",
        default_value: "hello",
      }),
    ];
    expect(buildScheduleParametersPayload({ greeting: "" }, params)).toBeNull();
  });

  test("drops empty file_url shaped objects for optional file_url params", () => {
    const params = [
      makeWorkflowParam({
        key: "doc",
        workflow_parameter_type: "file_url",
        default_value: "s3://default/key",
      }),
    ];
    expect(
      buildScheduleParametersPayload({ doc: { s3uri: "" } }, params),
    ).toBeNull();
  });

  test("preserves boolean false set by user when default is true", () => {
    const params = [
      makeWorkflowParam({
        key: "flag",
        workflow_parameter_type: "boolean",
        default_value: true,
      }),
    ];
    expect(buildScheduleParametersPayload({ flag: false }, params)).toEqual({
      flag: false,
    });
  });

  test("ignores values for keys not in the parameter list", () => {
    const params = [
      makeWorkflowParam({ key: "a", workflow_parameter_type: "string" }),
    ];
    expect(
      buildScheduleParametersPayload({ a: "hi", stale: "leftover" }, params),
    ).toEqual({ a: "hi" });
  });
});
