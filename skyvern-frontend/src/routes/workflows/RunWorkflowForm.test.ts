import { describe, expect, test } from "vitest";

import {
  getRunWorkflowRequestBody,
  isOverrideProfilePicked,
  type RunWorkflowFormType,
} from "./RunWorkflowForm";

function formValues(
  overrides: Partial<RunWorkflowFormType>,
): RunWorkflowFormType {
  return {
    webhookCallbackUrl: "",
    proxyLocation: "RESIDENTIAL",
    browserSessionId: null,
    browserProfileId: null,
    cdpAddress: null,
    maxScreenshotScrolls: null,
    extraHttpHeaders: null,
    cdpConnectHeaders: null,
    runWith: "agent",
    aiFallback: true,
    ...overrides,
  } as unknown as RunWorkflowFormType;
}

describe("getRunWorkflowRequestBody — start-fresh vs profile override (flag-on)", () => {
  test("start fresh drops the (settings-derived) profile override and sets the flag", () => {
    const body = getRunWorkflowRequestBody(
      formValues({ browserProfileId: "bp_x", startFreshBrowser: true }),
      [],
      undefined,
      true,
    );
    expect(body.browser_profile_id).toBeNull();
    expect(body.start_fresh_browser).toBe(true);
  });

  test("a profile override without start fresh is preserved", () => {
    const body = getRunWorkflowRequestBody(
      formValues({ browserProfileId: "bp_x", startFreshBrowser: false }),
      [],
      undefined,
      true,
    );
    expect(body.browser_profile_id).toBe("bp_x");
    expect(body.start_fresh_browser).toBe(false);
  });

  test("no override and no start fresh sends a null profile and a false flag", () => {
    const body = getRunWorkflowRequestBody(formValues({}), [], undefined, true);
    expect(body.browser_profile_id).toBeNull();
    expect(body.start_fresh_browser).toBe(false);
  });

  test("an attached live session suppresses start fresh (backend rejects the combo)", () => {
    const body = getRunWorkflowRequestBody(
      formValues({
        browserSessionId: "pbs_1",
        browserProfileId: "bp_x",
        startFreshBrowser: true,
      }),
      [],
      undefined,
      true,
    );
    expect(body.start_fresh_browser).toBe(false);
    expect(body.browser_session_id).toBe("pbs_1");
    expect(body.browser_profile_id).toBe("bp_x");
  });

  test("a per-input agent drops the rerun-seeded override (backend ranks it above the key)", () => {
    const body = getRunWorkflowRequestBody(
      formValues({ browserProfileId: "bp_x", startFreshBrowser: false }),
      [],
      "user_email",
      true,
    );
    expect(body.browser_profile_id).toBeNull();
  });

  test("a plain agent keeps the rerun-seeded override", () => {
    const body = getRunWorkflowRequestBody(
      formValues({ browserProfileId: "bp_x", startFreshBrowser: false }),
      [],
      null,
      true,
    );
    expect(body.browser_profile_id).toBe("bp_x");
  });
});

describe("getRunWorkflowRequestBody — flag-off legacy payload is byte-identical", () => {
  test("flag-off omits the browser-memory-only start_fresh_browser key and keeps the override", () => {
    const body = getRunWorkflowRequestBody(
      formValues({ browserProfileId: "bp_x", startFreshBrowser: false }),
      [],
      "user_email",
      false,
    );
    expect(body).toEqual({
      data: {},
      proxy_location: "RESIDENTIAL",
      browser_session_id: null,
      browser_profile_id: "bp_x",
      browser_address: null,
      run_with: "agent",
      ai_fallback: true,
    });
  });
});

describe("isOverrideProfilePicked — Start-fresh mutual exclusion", () => {
  test("a per-input rerun does not count as a picked override (Start-fresh stays enabled)", () => {
    expect(isOverrideProfilePicked("bp_x", "user_email", true)).toBe(false);
  });

  test("a plain rerun counts as a picked override (Start-fresh disabled)", () => {
    expect(isOverrideProfilePicked("bp_x", null, true)).toBe(true);
  });

  test("no override is never a picked override", () => {
    expect(isOverrideProfilePicked(null, "user_email", true)).toBe(false);
    expect(isOverrideProfilePicked("", null, true)).toBe(false);
  });
});
