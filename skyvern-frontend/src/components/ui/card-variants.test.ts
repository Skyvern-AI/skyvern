import { describe, expect, it } from "vitest";
import { cardVariants } from "./card-variants";

describe("cardVariants", () => {
  // Pre-cva Card class string was:
  //   "rounded-xl border bg-card text-card-foreground shadow"
  // Default tone MUST emit those same classes (order-insensitive — twMerge
  // resolves ordering when cn() composes them with caller className).
  it("returns the legacy class set for tone=default (existing-caller preservation)", () => {
    const result = cardVariants({ tone: "default" });
    expect(result).toContain("rounded-xl");
    expect(result).toContain("border");
    expect(result).toContain("bg-card");
    expect(result).toContain("text-card-foreground");
    expect(result).toContain("shadow");
  });

  it("defaults to tone=default when no tone is passed", () => {
    expect(cardVariants({})).toBe(cardVariants({ tone: "default" }));
  });

  it("emits a success-tinted border for tone=success", () => {
    const result = cardVariants({ tone: "success" });
    expect(result).toMatch(/border-success/);
  });

  it("emits a warning-tinted border for tone=warning", () => {
    const result = cardVariants({ tone: "warning" });
    expect(result).toMatch(/border-warning/);
  });

  it("emits a destructive-tinted border for tone=destructive", () => {
    const result = cardVariants({ tone: "destructive" });
    expect(result).toMatch(/border-destructive/);
  });

  it("keeps the rounded-xl + shadow + bg-card base across all tones (border-tint only)", () => {
    // Tones are border-tints only — full background washes were out-of-scope
    // per mandate. A destructive Card still reads as a Card, not a Banner.
    for (const tone of [
      "default",
      "success",
      "warning",
      "destructive",
    ] as const) {
      const result = cardVariants({ tone });
      expect(result).toContain("rounded-xl");
      expect(result).toContain("shadow");
      expect(result).toContain("bg-card");
    }
  });
});
