import { describe, expect, it } from "vitest";

import {
  ProxyLocation,
  type WorkflowRunStatusApiResponseWithWorkflow,
} from "@/api/types";
import type { Parameter, WorkflowParameter } from "./types/workflowTypes";
import {
  getInitialValues,
  getOrderedRunParameters,
  getRerunNavigationState,
  normalizeJsonParameterFormValue,
  parseJsonWorkflowParameterValue,
  validateJsonWorkflowParameterValue,
} from "./utils";

function buildWorkflowRun(
  overrides: Partial<WorkflowRunStatusApiResponseWithWorkflow> = {},
): WorkflowRunStatusApiResponseWithWorkflow {
  return {
    parameters: { query: "status report", payload: ["alpha"] },
    proxy_location: ProxyLocation.ResidentialDE,
    webhook_callback_url: "https://example.com/webhook",
    max_screenshot_scrolls: 8,
    run_with: "code",
    browser_profile_id: "profile_synthetic",
    extra_http_headers: { "X-Test": "synthetic" },
    ...overrides,
  } as WorkflowRunStatusApiResponseWithWorkflow;
}

describe("getRerunNavigationState", () => {
  it("maps exactly the six legacy rerun fields", () => {
    const state = getRerunNavigationState(buildWorkflowRun());

    expect(state).toEqual({
      data: { query: "status report", payload: ["alpha"] },
      proxyLocation: ProxyLocation.ResidentialDE,
      webhookCallbackUrl: "https://example.com/webhook",
      maxScreenshotScrolls: 8,
      runWith: "code",
      browserProfileId: "profile_synthetic",
    });
    expect(state).not.toHaveProperty("extraHttpHeaders");
    expect(state).not.toHaveProperty("cdpConnectHeaders");
    expect(state).not.toHaveProperty("cdpAddress");
  });

  it("matches the legacy nullish fallbacks", () => {
    const workflowRun = {
      parameters: null,
      proxy_location: null,
      webhook_callback_url: null,
      max_screenshot_scrolls: null,
      run_with: null,
      browser_profile_id: null,
    } as unknown as WorkflowRunStatusApiResponseWithWorkflow;

    expect(getRerunNavigationState(workflowRun)).toEqual({
      data: {},
      proxyLocation: ProxyLocation.Residential,
      webhookCallbackUrl: "",
      maxScreenshotScrolls: null,
      runWith: "agent",
      browserProfileId: null,
    });
  });

  it("flows rerun data through initial values with JSON normalization", () => {
    const state = getRerunNavigationState(buildWorkflowRun());
    const workflowParameters = [
      {
        parameter_type: "workflow",
        key: "query",
        workflow_parameter_type: "string",
      },
      {
        parameter_type: "workflow",
        key: "payload",
        workflow_parameter_type: "json",
      },
    ] as WorkflowParameter[];

    expect(
      getInitialValues(
        { state } as Parameters<typeof getInitialValues>[0],
        workflowParameters,
      ),
    ).toEqual({
      query: "status report",
      payload: '[\n  "alpha"\n]',
    });
  });
});

describe("parseJsonWorkflowParameterValue", () => {
  it("parses a JSON array string", () => {
    expect(parseJsonWorkflowParameterValue('["1002763917"]')).toEqual([
      "1002763917",
    ]);
  });

  it("returns a single-item array unchanged (SKY-10854)", () => {
    const value = ["1002763917"];
    expect(parseJsonWorkflowParameterValue(value)).toBe(value);
    expect(parseJsonWorkflowParameterValue(value)).toEqual(["1002763917"]);
  });

  it("returns multi-item arrays unchanged", () => {
    const value = ["a", "b"];
    expect(parseJsonWorkflowParameterValue(value)).toBe(value);
  });

  it("returns parsed objects unchanged", () => {
    const value = { ids: ["1002763917"] };
    expect(parseJsonWorkflowParameterValue(value)).toBe(value);
  });
});

describe("normalizeJsonParameterFormValue", () => {
  it("stringifies parsed arrays for form state", () => {
    expect(normalizeJsonParameterFormValue(["1002763917"])).toBe(
      '[\n  "1002763917"\n]',
    );
  });

  it("leaves strings unchanged", () => {
    expect(normalizeJsonParameterFormValue('["1002763917"]')).toBe(
      '["1002763917"]',
    );
  });

  it("keeps null as null for unset JSON params", () => {
    expect(normalizeJsonParameterFormValue(null)).toBeNull();
    expect(normalizeJsonParameterFormValue(undefined)).toBeNull();
  });
});

describe("validateJsonWorkflowParameterValue", () => {
  it("accepts null as valid JSON", () => {
    expect(validateJsonWorkflowParameterValue(null)).toBe(true);
    expect(validateJsonWorkflowParameterValue(undefined)).toBe(true);
  });

  it("accepts the null JSON literal string", () => {
    expect(validateJsonWorkflowParameterValue("null")).toBe(true);
  });

  it("accepts parsed arrays from re-run state", () => {
    expect(validateJsonWorkflowParameterValue(["1002763917"])).toBe(true);
  });

  it("rejects empty input", () => {
    expect(validateJsonWorkflowParameterValue("")).toBe(
      "This field is required",
    );
    expect(validateJsonWorkflowParameterValue("   ")).toBe(
      "This field is required",
    );
  });

  it("rejects invalid JSON", () => {
    expect(validateJsonWorkflowParameterValue("{not json")).toBe(
      "Invalid JSON",
    );
  });
});

describe("getOrderedRunParameters", () => {
  it("orders by the workflow definition, then appends definition-less extras", () => {
    const definitionParameters = [
      { parameter_type: "workflow", key: "first", description: "First field" },
      { parameter_type: "workflow", key: "second", description: null },
    ] as unknown as Parameter[];
    const runParameters = { second: "b", first: "a", extra: "c" };

    const result = getOrderedRunParameters(definitionParameters, runParameters);

    expect(result.map(([key]) => key)).toEqual(["first", "second", "extra"]);
    expect(result.map(([, value]) => value)).toEqual(["a", "b", "c"]);
    // The matched definition rides along per key.
    expect(result.map(([, , def]) => def?.description ?? null)).toEqual([
      "First field",
      null,
      null,
    ]);
    // Extras (absent from the definition) carry no definition object.
    expect(result[2]?.[2]).toBeUndefined();
  });

  it("never surfaces credential/secret parameter definitions", () => {
    const definitionParameters = [
      {
        parameter_type: "workflow",
        key: "invoice_url",
        description: "Invoice",
      },
      {
        parameter_type: "credential",
        key: "login",
        credential_id: "cred_secret_123",
      },
    ] as unknown as Parameter[];
    const runParameters = { invoice_url: "https://x.test" };

    const result = getOrderedRunParameters(definitionParameters, runParameters);

    // Only the workflow parameter is surfaced; the credential key is absent...
    expect(result.map(([key]) => key)).toEqual(["invoice_url"]);
    // ...and no credential-definition metadata leaks into any entry.
    expect(JSON.stringify(result)).not.toContain("cred_secret_123");
  });

  it("falls back to Object.entries ordering without a definition", () => {
    expect(getOrderedRunParameters(undefined, { b: 2, a: 1 })).toEqual([
      ["b", 2, undefined],
      ["a", 1, undefined],
    ]);
  });
});
