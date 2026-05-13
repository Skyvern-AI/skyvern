import { describe, expect, it } from "vitest";
import { skeletonVariants } from "./skeleton-variants";

describe("skeletonVariants", () => {
  it("returns the legacy class string for variant=rect", () => {
    const result = skeletonVariants({ variant: "rect" });
    expect(result).toContain("animate-pulse");
    expect(result).toContain("rounded-md");
    expect(result).toContain("bg-primary/10");
  });

  it("defaults to rect when no variant is passed (existing-caller preservation)", () => {
    expect(skeletonVariants({})).toBe(skeletonVariants({ variant: "rect" }));
  });

  it("emits rounded-full for circle so twMerge collapses rounded-md", () => {
    expect(skeletonVariants({ variant: "circle" })).toContain("rounded-full");
  });

  it("emits a flex-col container for the text variant so stacked lines align", () => {
    expect(skeletonVariants({ variant: "text" })).toContain("flex");
    expect(skeletonVariants({ variant: "text" })).toContain("flex-col");
  });
});
