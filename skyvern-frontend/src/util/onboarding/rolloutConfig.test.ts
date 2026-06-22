import { describe, expect, it } from "vitest";
import {
  RAMP_STAGES,
  GUARDRAIL_METRICS,
  ROLLBACK_RULES,
  DASHBOARD_WIDGETS,
  ROLLOUT_CONFIG,
  getAutoRollbackRules,
  getGuardrailForMetric,
  getMetricDefinition,
  isGateCheckRequired,
} from "./rolloutConfig";
import { EDITOR_ONBOARDING_TOUR_FLAG } from "@/util/featureFlags";

describe("rolloutConfig", () => {
  describe("RAMP_STAGES", () => {
    it("has 4 stages: 0% -> 10% -> 50% -> 100%", () => {
      expect(RAMP_STAGES).toHaveLength(4);
      const percentages = RAMP_STAGES.map((s) => s.percentage);
      expect(percentages).toEqual([0, 10, 50, 100]);
    });

    it("stages are monotonically increasing", () => {
      for (let i = 1; i < RAMP_STAGES.length; i++) {
        expect(RAMP_STAGES[i]!.percentage).toBeGreaterThan(
          RAMP_STAGES[i - 1]!.percentage,
        );
      }
    });

    it("canary holds for 1 day, broad holds for 3 days", () => {
      const canary = RAMP_STAGES.find((s) => s.name === "canary");
      const broad = RAMP_STAGES.find((s) => s.name === "broad");
      expect(canary?.holdDays).toBe(1);
      expect(broad?.holdDays).toBe(3);
    });

    it("gate checks required at canary and broad stages", () => {
      expect(isGateCheckRequired(1)).toBe(true);
      expect(isGateCheckRequired(2)).toBe(true);
      expect(isGateCheckRequired(0)).toBe(false);
      expect(isGateCheckRequired(3)).toBe(false);
    });
  });

  describe("GUARDRAIL_METRICS", () => {
    it("defines error rate, modal render, tour JS, and completion drop", () => {
      const ids = GUARDRAIL_METRICS.map((g) => g.id);
      expect(ids).toContain("onboarding_error_rate");
      expect(ids).toContain("modal_render_failure_rate");
      expect(ids).toContain("tour_js_error_rate");
      expect(ids).toContain("completion_rate_drop");
    });

    it("all error-rate guardrails threshold at 5%", () => {
      const errorGuardrails = GUARDRAIL_METRICS.filter(
        (g) => g.id.endsWith("_rate") && g.direction === "above",
      );
      for (const g of errorGuardrails) {
        expect(g.threshold).toBe(0.05);
      }
    });

    it("completion rate drop threshold is 20%", () => {
      const drop = GUARDRAIL_METRICS.find(
        (g) => g.id === "completion_rate_drop",
      );
      expect(drop?.threshold).toBe(0.2);
      expect(drop?.direction).toBe("below");
    });
  });

  describe("ROLLBACK_RULES", () => {
    it("all rules trigger auto-rollback", () => {
      for (const rule of ROLLBACK_RULES) {
        expect(rule.action).toBe("auto-rollback");
      }
    });

    it("every rule references a defined guardrail metric", () => {
      const guardrailIds = new Set(GUARDRAIL_METRICS.map((g) => g.id));
      for (const rule of ROLLBACK_RULES) {
        expect(guardrailIds.has(rule.metric)).toBe(true);
      }
    });

    it("getAutoRollbackRules returns all rules", () => {
      expect(getAutoRollbackRules()).toEqual(ROLLBACK_RULES);
    });
  });

  describe("DASHBOARD_WIDGETS", () => {
    it("has 6 widgets", () => {
      expect(DASHBOARD_WIDGETS).toHaveLength(6);
    });

    it("includes required widgets from AC", () => {
      const ids = DASHBOARD_WIDGETS.map((w) => w.id);
      expect(ids).toContain("activation_funnel_by_variant");
      expect(ids).toContain("tour_completion_rate");
      expect(ids).toContain("modal_skip_rate");
      expect(ids).toContain("time_to_first_value_distribution");
    });

    it("activation funnel widget has variant breakdown", () => {
      const funnel = DASHBOARD_WIDGETS.find(
        (w) => w.id === "activation_funnel_by_variant",
      );
      expect(funnel?.breakdownBy).toBe("variant");
      expect(funnel?.kind).toBe("funnel");
    });
  });

  describe("ROLLOUT_CONFIG", () => {
    it("references the correct feature flag", () => {
      expect(ROLLOUT_CONFIG.flagKey).toBe(EDITOR_ONBOARDING_TOUR_FLAG);
    });
  });

  describe("helpers", () => {
    it("getGuardrailForMetric finds by id", () => {
      const g = getGuardrailForMetric("onboarding_error_rate");
      expect(g?.threshold).toBe(0.05);
    });

    it("getGuardrailForMetric returns undefined for unknown id", () => {
      expect(getGuardrailForMetric("nonexistent")).toBeUndefined();
    });

    it("getMetricDefinition finds experiment metrics", () => {
      const m = getMetricDefinition("activation_rate");
      expect(m?.priority).toBe("primary");
    });

    it("isGateCheckRequired returns false for out-of-bounds", () => {
      expect(isGateCheckRequired(99)).toBe(false);
    });
  });
});
