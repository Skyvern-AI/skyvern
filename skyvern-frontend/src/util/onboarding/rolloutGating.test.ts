import { describe, expect, it } from "vitest";
import {
  isActivationRun,
  isFirstFailedRunRecoveryEligible,
} from "./rolloutGating";

describe("isActivationRun", () => {
  const loaded = { isLoading: false, state: { first_run_at: null } };

  it("is true for an A/B arm when the user has no completed first run", () => {
    expect(isActivationRun("template-first", loaded)).toBe(true);
    expect(isActivationRun("copilot-first", loaded)).toBe(true);
  });

  it("is false when the flag is off (rollback / 0% rollout)", () => {
    expect(isActivationRun(false, loaded)).toBe(false);
    expect(isActivationRun(undefined, loaded)).toBe(false);
    expect(isActivationRun("control", loaded)).toBe(false);
  });

  it("is false once a first run exists", () => {
    expect(
      isActivationRun("template-first", {
        isLoading: false,
        state: { first_run_at: "2026-01-01T00:00:00Z" },
      }),
    ).toBe(false);
  });

  it("is false while loading or without state", () => {
    expect(
      isActivationRun("template-first", {
        isLoading: true,
        state: { first_run_at: null },
      }),
    ).toBe(false);
    expect(
      isActivationRun("template-first", { isLoading: false, state: null }),
    ).toBe(false);
    expect(isActivationRun("template-first", null)).toBe(false);
  });
});

describe("isFirstFailedRunRecoveryEligible", () => {
  const base = {
    flagVariant: "template-first" as string | boolean | undefined,
    isNewUser: true,
    isFailureRun: true,
    hasFailureReason: true,
  };

  it("is true for a new user's failed first run under an A/B arm", () => {
    expect(isFirstFailedRunRecoveryEligible(base)).toBe(true);
  });

  it("is false when the flag is off", () => {
    expect(
      isFirstFailedRunRecoveryEligible({ ...base, flagVariant: false }),
    ).toBe(false);
    expect(
      isFirstFailedRunRecoveryEligible({ ...base, flagVariant: undefined }),
    ).toBe(false);
  });

  it("is false for existing users or non-failures", () => {
    expect(
      isFirstFailedRunRecoveryEligible({ ...base, isNewUser: false }),
    ).toBe(false);
    expect(
      isFirstFailedRunRecoveryEligible({ ...base, isFailureRun: false }),
    ).toBe(false);
    expect(
      isFirstFailedRunRecoveryEligible({ ...base, hasFailureReason: false }),
    ).toBe(false);
  });
});
