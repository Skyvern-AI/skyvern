import { describe, expect, it } from "vitest";
import {
  EXPERIMENT,
  ACTIVATION_FUNNEL,
  METRICS,
  POWER_ANALYSIS,
  VARIANTS,
  VARIANT_LIST,
  DEFAULT_VARIANT,
  isABVariant,
} from "./experimentConfig";

describe("experimentConfig", () => {
  it("has two equally-weighted variants", () => {
    expect(VARIANT_LIST).toHaveLength(2);
    expect(VARIANT_LIST).toContain(VARIANTS.TEMPLATE_FIRST);
    expect(VARIANT_LIST).toContain(VARIANTS.COPILOT_FIRST);
    expect(EXPERIMENT.rolloutWeights["template-first"]).toBe(50);
    expect(EXPERIMENT.rolloutWeights["copilot-first"]).toBe(50);
  });

  it("defaults to template-first", () => {
    expect(DEFAULT_VARIANT).toBe(VARIANTS.TEMPLATE_FIRST);
  });

  it("funnel has 4 ordered steps", () => {
    expect(ACTIVATION_FUNNEL).toHaveLength(4);
    const orders = ACTIVATION_FUNNEL.map((s) => s.order);
    expect(orders).toEqual([1, 2, 3, 4]);
  });

  it("funnel events reference valid onboarding telemetry events", () => {
    for (const step of ACTIVATION_FUNNEL) {
      expect(step.event).toMatch(/^onboarding\./);
    }
  });

  it("has exactly one primary metric", () => {
    const primary = METRICS.filter((m) => m.priority === "primary");
    expect(primary).toHaveLength(1);
    expect(primary[0]!.id).toBe("activation_rate");
  });

  it("has three secondary metrics", () => {
    const secondary = METRICS.filter((m) => m.priority === "secondary");
    expect(secondary).toHaveLength(3);
    const ids = secondary.map((m) => m.id);
    expect(ids).toContain("time_to_first_value");
    expect(ids).toContain("onboarding_completion_rate");
    expect(ids).toContain("seven_day_retention");
  });

  it("power analysis requires ~4400 total samples", () => {
    expect(POWER_ANALYSIS.samplesPerVariant).toBeGreaterThanOrEqual(2000);
    expect(POWER_ANALYSIS.totalSamplesRequired).toBe(
      POWER_ANALYSIS.samplesPerVariant * VARIANT_LIST.length,
    );
  });

  describe("isABVariant", () => {
    it("accepts valid variants", () => {
      expect(isABVariant("template-first")).toBe(true);
      expect(isABVariant("copilot-first")).toBe(true);
    });

    it("rejects invalid values", () => {
      expect(isABVariant("control")).toBe(false);
      expect(isABVariant(null)).toBe(false);
      expect(isABVariant(undefined)).toBe(false);
      expect(isABVariant(42)).toBe(false);
    });
  });
});
