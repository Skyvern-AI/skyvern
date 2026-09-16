import { renderHook } from "@testing-library/react";
import { act } from "react";
import { useForm } from "react-hook-form";
import { describe, expect, it } from "vitest";

import {
  ProxyLocation,
  type WorkflowRunStatusApiResponseWithWorkflow,
} from "@/api/types";
import type {
  Parameter,
  WorkflowApiResponse,
  WorkflowParameter,
  WorkflowSettings,
} from "./types/workflowTypes";
import {
  browserTypeSelectionDisabled,
  extractBrowserTypeSetting,
  getInitialValues,
  getOrderedRunParameters,
  getRerunNavigationState,
  hasBrowserTypeOptions,
  RESERVED_BROWSER_TYPE_FIELD,
  rerunBrowserTypeState,
  resolveInitialBrowserType,
  normalizeJsonParameterFormValue,
  parseJsonWorkflowParameterValue,
  shouldPollForGeneratedCode,
  validateJsonWorkflowParameterValue,
} from "./utils";
import { getRunWorkflowRequestBody } from "./RunWorkflowForm";
import {
  snapshotOf,
  summarizeWorkflowChanges,
} from "./editor/workflowChangesSummary";
import type { WorkflowSaveData } from "@/store/WorkflowHasChangesStore";

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
    browser_type: "msedge",
    extra_http_headers: { "X-Test": "synthetic" },
    ...overrides,
  } as WorkflowRunStatusApiResponseWithWorkflow;
}

function buildWorkflow(
  blockLabels: string[],
  finallyBlockLabel: string | null = null,
): WorkflowApiResponse {
  return {
    workflow_definition: {
      blocks: blockLabels.map((label) => ({ label })),
      finally_block_label: finallyBlockLabel,
    },
  } as WorkflowApiResponse;
}

describe("shouldPollForGeneratedCode", () => {
  it("polls only while an executable workflow can still produce code", () => {
    expect(shouldPollForGeneratedCode(buildWorkflow([]), false, false)).toBe(
      false,
    );
    expect(
      shouldPollForGeneratedCode(
        buildWorkflow(["cleanup"], "cleanup"),
        false,
        false,
      ),
    ).toBe(false);
    expect(
      shouldPollForGeneratedCode(buildWorkflow(["body"]), false, false),
    ).toBe(true);
    expect(
      shouldPollForGeneratedCode(buildWorkflow(["body"]), true, false),
    ).toBe(false);
    expect(
      shouldPollForGeneratedCode(buildWorkflow(["body"]), false, true),
    ).toBe(false);
  });
});

describe("rerunBrowserTypeState (shared by all Rerun/Retry callers)", () => {
  it("carries a real browser_type value", () => {
    expect(rerunBrowserTypeState({ browser_type: "msedge" })).toEqual({
      browserType: "msedge",
    });
  });
  it("carries explicit null as browserType: null", () => {
    expect(rerunBrowserTypeState({ browser_type: null })).toEqual({
      browserType: null,
    });
  });
  it("omits browserType entirely when the property is absent (older backend/legacy)", () => {
    const state = rerunBrowserTypeState({});
    expect(state).not.toHaveProperty("browserType");
    expect(state).toEqual({});
  });
});

describe("resolveInitialBrowserType (null inherits the workflow setting)", () => {
  it("preselects the executed run's non-null browser_type carried in state", () => {
    expect(resolveInitialBrowserType({ browserType: "msedge" }, "chrome")).toBe(
      "msedge",
    );
  });

  it("inherits the current workflow default when the run's browser_type is null", () => {
    // Contract: a run-level null means inherit the workflow setting (no "force default" sentinel).
    // The source run ran with null; the workflow default is now chrome, so the rerun uses chrome —
    // matching how the backend resolves an omitted/null run value.
    expect(resolveInitialBrowserType({ browserType: null }, "chrome")).toBe(
      "chrome",
    );
  });

  it("inherits the workflow default when state never carried the property (older backend)", () => {
    expect(resolveInitialBrowserType({}, "chrome")).toBe("chrome");
    expect(resolveInitialBrowserType(null, "chrome")).toBe("chrome");
    expect(resolveInitialBrowserType(undefined, "chrome")).toBe("chrome");
  });

  it("resolves to null only when both the run value and the workflow are null (dynamic routing)", () => {
    expect(resolveInitialBrowserType({ browserType: null }, null)).toBeNull();
    expect(resolveInitialBrowserType({}, null)).toBeNull();
    expect(resolveInitialBrowserType({}, undefined)).toBeNull();
  });
});

describe("reserved browser-type form field", () => {
  it("react-hook-form treats the hyphenated reserved name as a flat top-level key", () => {
    const { result } = renderHook(() =>
      useForm<Record<string, unknown>>({
        defaultValues: { [RESERVED_BROWSER_TYPE_FIELD]: null },
      }),
    );

    act(() => {
      result.current.register(RESERVED_BROWSER_TYPE_FIELD);
      result.current.setValue(RESERVED_BROWSER_TYPE_FIELD, "msedge");
    });

    expect(result.current.getValues(RESERVED_BROWSER_TYPE_FIELD)).toBe(
      "msedge",
    );
    expect(result.current.watch(RESERVED_BROWSER_TYPE_FIELD)).toBe("msedge");
    // Flat key, not a nested path: no accidental `__skyvern-browser-type` object graph.
    const all = result.current.getValues();
    expect(all[RESERVED_BROWSER_TYPE_FIELD]).toBe("msedge");
  });

  it("extracts the reserved setting while preserving a user parameter literally named browserType", () => {
    const { browserType, rest } = extractBrowserTypeSetting({
      [RESERVED_BROWSER_TYPE_FIELD]: "msedge",
      browserType: "business-value",
      other: 1,
    });

    // The internal setting becomes the API browser_type source...
    expect(browserType).toBe("msedge");
    // ...and the reserved key is stripped from the request data...
    expect(rest).not.toHaveProperty(RESERVED_BROWSER_TYPE_FIELD);
    // ...while the legitimate workflow parameter survives unchanged.
    expect(rest.browserType).toBe("business-value");
    expect(rest.other).toBe(1);
  });

  it("treats an absent or null reserved setting as Default (null)", () => {
    expect(extractBrowserTypeSetting({}).browserType).toBeNull();
    expect(
      extractBrowserTypeSetting({ [RESERVED_BROWSER_TYPE_FIELD]: null })
        .browserType,
    ).toBeNull();
  });
});

describe("browserTypeSelectionDisabled (an attachment owns the engine)", () => {
  it("is true when only a session is set", () => {
    expect(browserTypeSelectionDisabled({ browserSessionId: "pbs_abc" })).toBe(
      true,
    );
  });

  it("is true when only a remote address is set", () => {
    expect(
      browserTypeSelectionDisabled({ browserAddress: "http://host:9222" }),
    ).toBe(true);
  });

  it("is true when both are set", () => {
    expect(
      browserTypeSelectionDisabled({
        browserSessionId: "pbs_abc",
        browserAddress: "http://host:9222",
      }),
    ).toBe(true);
  });

  it("is false when neither is set (including blank/whitespace); a browser profile is not an attachment", () => {
    expect(browserTypeSelectionDisabled({})).toBe(false);
    expect(
      browserTypeSelectionDisabled({
        browserSessionId: null,
        browserAddress: null,
      }),
    ).toBe(false);
    expect(
      browserTypeSelectionDisabled({
        browserSessionId: "  ",
        browserAddress: "  ",
      }),
    ).toBe(false);
  });
});

describe("hasBrowserTypeOptions (render the selector only with real options)", () => {
  it("is false while the query is loading or errored (options undefined)", () => {
    expect(hasBrowserTypeOptions(undefined)).toBe(false);
  });

  it("is false when the backend returns an empty list", () => {
    expect(hasBrowserTypeOptions([])).toBe(false);
  });

  it("is true once at least one option exists", () => {
    expect(
      hasBrowserTypeOptions([{ value: "msedge", label: "Microsoft Edge" }]),
    ).toBe(true);
  });
});

describe("getRunWorkflowRequestBody browser_type suppression rule", () => {
  function buildFormValues(
    overrides: Record<string, unknown> = {},
  ): Parameters<typeof getRunWorkflowRequestBody>[0] {
    return {
      webhookCallbackUrl: "",
      proxyLocation: ProxyLocation.Residential,
      browserSessionId: null,
      reuseBrowserSession: null,
      browserProfileId: null,
      startFreshBrowser: false,
      cdpAddress: null,
      maxScreenshotScrolls: null,
      extraHttpHeaders: null,
      cdpConnectHeaders: null,
      runWith: "agent",
      aiFallback: true,
      [RESERVED_BROWSER_TYPE_FIELD]: "msedge",
      ...overrides,
    } as Parameters<typeof getRunWorkflowRequestBody>[0];
  }

  it("sends browser_type when neither a session nor an address is attached", () => {
    const body = getRunWorkflowRequestBody(buildFormValues(), []);
    expect(body.browser_type).toBe("msedge");
  });

  it("omits browser_type when a session is attached even if the field held a value", () => {
    const body = getRunWorkflowRequestBody(
      buildFormValues({ browserSessionId: "pbs_abc" }),
      [],
    );
    expect(body).not.toHaveProperty("browser_type");
  });

  it("omits browser_type when a remote address is attached", () => {
    const body = getRunWorkflowRequestBody(
      buildFormValues({ cdpAddress: "http://host:9222" }),
      [],
    );
    expect(body).not.toHaveProperty("browser_type");
  });

  it("normalizes a cleared browser address to null so it does not conflict with a selected browser_type", () => {
    const body = getRunWorkflowRequestBody(
      buildFormValues({ cdpAddress: "" }),
      [],
    );
    // A cleared address is not an attachment, so the selected engine still applies...
    expect(body.browser_type).toBe("msedge");
    // ...and the empty string must become null; otherwise the backend treats ""
    // as an attachment (browser_address is not None) and rejects the run with 422.
    expect(body.browser_address).toBeNull();
  });

  it("keeps a legitimate `browserType` workflow parameter in request data unchanged", () => {
    const workflowParameters = [
      {
        parameter_type: "workflow",
        key: "browserType",
        workflow_parameter_type: "string",
      },
    ] as WorkflowParameter[];
    const body = getRunWorkflowRequestBody(
      buildFormValues({
        browserSessionId: "pbs_abc",
        browserType: "business-value",
      }),
      workflowParameters,
    );
    // The reserved internal setting is suppressed by the attachment...
    expect(body).not.toHaveProperty("browser_type");
    // ...while the user's workflow parameter literally named browserType survives.
    expect(body.data.browserType).toBe("business-value");
  });
});

describe("browser-type change summary", () => {
  function buildSettings(
    overrides: Partial<WorkflowSettings> = {},
  ): WorkflowSettings {
    return {
      retryPolicy: null,
      proxyLocation: null,
      webhookCallbackUrl: null,
      persistBrowserSession: false,
      reuseBrowserSession: false,
      pinSavedSessionIp: false,
      browserProfileId: null,
      browserProfileKey: null,
      model: null,
      maxScreenshotScrolls: null,
      maxElapsedTimeMinutes: null,
      extraHttpHeaders: null,
      cdpConnectHeaders: null,
      runWith: "agent",
      browserType: null,
      codeVersion: null,
      scriptCacheKey: null,
      aiFallback: null,
      enableSelfHealing: null,
      maskSecrets: false,
      runSequentially: false,
      sequentialKey: null,
      finallyBlockLabel: null,
      workflowSystemPrompt: null,
      errorCodeMapping: null,
      ...overrides,
    };
  }

  function buildSaveData(settings: WorkflowSettings): WorkflowSaveData {
    return {
      blocks: [],
      parameters: [],
      settings,
      title: "wf",
    } as unknown as WorkflowSaveData;
  }

  it("emits the browser-type-specific label when only browserType changes", () => {
    const baseline = buildSaveData(buildSettings({ browserType: null }));
    const draft = buildSaveData(buildSettings({ browserType: "msedge" }));

    const changes = summarizeWorkflowChanges(draft, snapshotOf(baseline));

    expect(changes).toContain("Changed browser type");
    expect(changes).not.toContain("Other workflow changes");
  });

  it("does not report a browser-type change when it is unchanged", () => {
    const baseline = buildSaveData(buildSettings({ browserType: "chrome" }));
    const draft = buildSaveData(buildSettings({ browserType: "chrome" }));

    const changes = summarizeWorkflowChanges(draft, snapshotOf(baseline));

    expect(changes).not.toContain("Changed browser type");
  });
});

describe("getRerunNavigationState", () => {
  it("maps the legacy rerun fields plus the executed run's browser_type", () => {
    const state = getRerunNavigationState(buildWorkflowRun());

    expect(state).toEqual({
      data: { query: "status report", payload: ["alpha"] },
      proxyLocation: ProxyLocation.ResidentialDE,
      webhookCallbackUrl: "https://example.com/webhook",
      maxScreenshotScrolls: 8,
      runWith: "code",
      browserProfileId: "profile_synthetic",
      browserType: "msedge",
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
      browser_type: null,
    } as unknown as WorkflowRunStatusApiResponseWithWorkflow;

    expect(getRerunNavigationState(workflowRun)).toEqual({
      data: {},
      proxyLocation: ProxyLocation.Residential,
      webhookCallbackUrl: "",
      maxScreenshotScrolls: null,
      runWith: "agent",
      browserProfileId: null,
      browserType: null,
    });
  });

  it("omits browserType entirely when the source has no browser_type property (legacy/older backend)", () => {
    const workflowRun = {
      parameters: null,
      proxy_location: null,
      webhook_callback_url: null,
      max_screenshot_scrolls: null,
      run_with: null,
      browser_profile_id: null,
      // no browser_type property at all — an older backend or legacy fixture
    } as unknown as WorkflowRunStatusApiResponseWithWorkflow;

    const state = getRerunNavigationState(workflowRun);
    expect(state).not.toHaveProperty("browserType");
    expect(state).toEqual({
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
