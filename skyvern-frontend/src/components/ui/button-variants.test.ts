import { describe, expect, it } from "vitest";
import { buttonVariants } from "./button-variants";

describe("buttonVariants v2 tokenization", () => {
  it("destructive uses --destructive token, not bg-red-900 literal", () => {
    const result = buttonVariants({ variant: "destructive" });
    expect(result).toContain("bg-destructive");
    expect(result).not.toMatch(/bg-red-\d/);
  });

  it("tertiary border is tokenized, not border-slate-NNN", () => {
    const result = buttonVariants({ variant: "tertiary" });
    expect(result).not.toMatch(/border-slate-\d/);
  });

  it("default still uses bg-primary text-primary-foreground", () => {
    const result = buttonVariants({ variant: "default" });
    expect(result).toContain("bg-primary");
    expect(result).toContain("text-primary-foreground");
  });
});

describe("buttonVariants brand variant", () => {
  it("brand variant uses brand-cta tokens and shadow", () => {
    const result = buttonVariants({ variant: "brand" });
    expect(result).toContain("bg-brand-cta");
    expect(result).toContain("text-brand-cta-foreground");
    expect(result).toContain("shadow-sm");
  });
});
