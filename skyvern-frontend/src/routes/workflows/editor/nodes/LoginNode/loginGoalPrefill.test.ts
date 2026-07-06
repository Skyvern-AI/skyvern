import { describe, expect, it } from "vitest";

import { computeLoginGoalPrefill } from "./loginGoalPrefill";
import { loginNodeDefaultData } from "./types";

const DEFAULT_GOAL = loginNodeDefaultData.navigationGoal;
const CONTEXT_A = "Click the SSO button first, then enter Google credentials";
const CONTEXT_B = "Use the magic-link email flow instead of a password";

describe("computeLoginGoalPrefill", () => {
  it("returns null when the credential has no instructions and the goal is untouched", () => {
    expect(computeLoginGoalPrefill(DEFAULT_GOAL, null)).toBeNull();
    expect(computeLoginGoalPrefill(DEFAULT_GOAL, undefined)).toBeNull();
    expect(computeLoginGoalPrefill(DEFAULT_GOAL, "   ")).toBeNull();
    expect(computeLoginGoalPrefill("", null)).toBeNull();
  });

  it("prefills when the goal is still the untouched default", () => {
    const result = computeLoginGoalPrefill(DEFAULT_GOAL, CONTEXT_A);
    expect(result).not.toBeNull();
    expect(result!.startsWith(DEFAULT_GOAL.trimEnd())).toBe(true);
    expect(result).toContain("\n\nADDITIONAL CONTEXT FROM THE USER");
    expect(result!.endsWith(CONTEXT_A)).toBe(true);
  });

  it("prefills when the goal is empty", () => {
    const result = computeLoginGoalPrefill("", CONTEXT_A);
    expect(result).not.toBeNull();
    expect(result!.startsWith(DEFAULT_GOAL.trimEnd())).toBe(true);
    expect(result!.endsWith(CONTEXT_A)).toBe(true);
  });

  it("does not clobber a goal the user customized", () => {
    expect(
      computeLoginGoalPrefill("My own bespoke login steps", CONTEXT_A),
    ).toBeNull();
  });

  it("replaces a prior credential's generated instructions when switching credentials", () => {
    const withA = computeLoginGoalPrefill(DEFAULT_GOAL, CONTEXT_A);
    expect(withA).not.toBeNull();

    const withB = computeLoginGoalPrefill(withA!, CONTEXT_B);
    expect(withB).not.toBeNull();
    expect(withB).not.toContain(CONTEXT_A);
    expect(withB!.endsWith(CONTEXT_B)).toBe(true);
    expect(withB!.startsWith(DEFAULT_GOAL.trimEnd())).toBe(true);
  });

  it("restores the plain default when switching to a credential with no instructions", () => {
    const withA = computeLoginGoalPrefill(DEFAULT_GOAL, CONTEXT_A);
    expect(withA).not.toBeNull();

    const cleared = computeLoginGoalPrefill(withA!, null);
    expect(cleared).toBe(DEFAULT_GOAL);
  });

  it("is idempotent — reselecting the same credential does not re-append", () => {
    const withA = computeLoginGoalPrefill(DEFAULT_GOAL, CONTEXT_A);
    expect(withA).not.toBeNull();
    expect(computeLoginGoalPrefill(withA!, CONTEXT_A)).toBeNull();
  });

  it("trims surrounding whitespace on the instructions", () => {
    const result = computeLoginGoalPrefill(DEFAULT_GOAL, `  ${CONTEXT_A}  `);
    expect(result!.endsWith(CONTEXT_A)).toBe(true);
  });
});
