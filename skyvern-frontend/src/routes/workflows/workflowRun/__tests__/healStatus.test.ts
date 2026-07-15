import { describe, expect, it } from "vitest";

import {
  healChipTooltip,
  healEngineEmphasis,
  healEngineLabel,
  healPanelInvariant,
  healSkipReasonLabel,
  healStatusHue,
  healStatusLabel,
} from "../healStatus";

describe("heal status helpers", () => {
  it("maps backend status values to user-facing labels", () => {
    expect(healStatusLabel("fired_completed")).toBe("Self-healed");
    expect(healStatusLabel("fired_unverified")).toBe("Recovered · unverified");
    expect(healStatusLabel("fired_failed")).toBe("Heal failed");
    expect(healStatusLabel("skipped")).toBe("No heal");
  });

  it("reserves the success hue for completed heals only (no false green)", () => {
    expect(healStatusHue("fired_completed")).toBe("success");
    expect(healStatusHue("fired_unverified")).not.toBe("success");
    expect(healStatusHue("fired_failed")).not.toBe("success");
    expect(healStatusHue("skipped")).not.toBe("success");
  });

  it("colors a failed heal orange, never red — the run status owns red", () => {
    expect(healStatusHue("fired_failed")).toBe("orange");
  });

  it("de-emphasizes fallback-engine (floor) recoveries vs harness", () => {
    expect(healEngineEmphasis("harness")).toBe("solid");
    expect(healEngineEmphasis("code")).toBe("solid");
    expect(healEngineEmphasis("floor")).toBe("soft");
  });

  it("labels engines in human terms, not internal jargon", () => {
    expect(healEngineLabel("harness")).toBe("primary");
    expect(healEngineLabel("floor")).toBe("fallback");
    expect(healEngineLabel("code")).toBe("primary");
  });

  it("maps known skip reasons to plain-language labels", () => {
    expect(healSkipReasonLabel("capped")).toBe("Attempt limit reached");
    expect(healSkipReasonLabel("adoption_failed")).toBe("Recovery not adopted");
    expect(healSkipReasonLabel("credential_unavailable")).toBe(
      "Credential unavailable",
    );
    expect(healSkipReasonLabel("timeout_class")).toBe("Timed out");
    expect(healSkipReasonLabel("insecure_code")).toBe("Unsafe code blocked");
    expect(healSkipReasonLabel("unclassifiable")).toBe("Unclassified");
  });

  it("renders a dash for a null skip reason", () => {
    expect(healSkipReasonLabel(null)).toBe("-");
  });

  it("de-snakes unknown skip reasons so no raw enum ever renders", () => {
    expect(healSkipReasonLabel("some_future_reason")).toBe(
      "some future reason",
    );
  });

  it("only claims recovery in copy when something actually recovered", () => {
    expect(healPanelInvariant(true)).toBe(
      "Recovered this run — your workflow version is unchanged.",
    );
    expect(healPanelInvariant(false)).toBe(
      "Your workflow version is unchanged.",
    );
    expect(healPanelInvariant(false)).not.toContain("Recovered");
    expect(healChipTooltip(true)).toContain("recovered this run");
    expect(healChipTooltip(false)).toContain("attempted");
    expect(healChipTooltip(false)).not.toContain("recovered this run");
  });
});
