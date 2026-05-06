import { describe, expect, it } from "vitest";
import { tableVariants } from "./table-variants";

describe("tableVariants", () => {
  // Pre-cva Table class string was:
  //   "w-full caption-bottom text-sm"
  // The default variant must emit those classes so existing Table callers
  // (many — Table is a workhorse) render byte-identical output.
  it("returns the legacy class set for variant=default (existing-caller preservation)", () => {
    const result = tableVariants({ variant: "default" });
    expect(result).toContain("w-full");
    expect(result).toContain("caption-bottom");
    expect(result).toContain("text-sm");
  });

  it("defaults to variant=default when no variant is passed", () => {
    expect(tableVariants({})).toBe(tableVariants({ variant: "default" }));
  });

  it("does not inject column-width selectors on the default variant", () => {
    // The line-5col selectors must NOT bleed into the default variant —
    // existing callers don't expect their TableCell to be auto-aligned.
    const result = tableVariants({ variant: "default" });
    expect(result).not.toMatch(/nth-child/);
    expect(result).not.toMatch(/tabular-nums/);
  });

  describe("variant=line-5col schema", () => {
    const result = tableVariants({ variant: "line-5col" });

    it("preserves the legacy base classes", () => {
      expect(result).toContain("w-full");
      expect(result).toContain("caption-bottom");
      expect(result).toContain("text-sm");
    });

    it("right-aligns numeric columns 2-5 in headers and cells", () => {
      expect(result).toMatch(/\[&_th:nth-child\(n\+2\)\]:text-right/);
      expect(result).toMatch(/\[&_td:nth-child\(n\+2\)\]:text-right/);
    });

    it("applies tabular-nums to the numeric cells (cols 2-5)", () => {
      expect(result).toMatch(/\[&_td:nth-child\(n\+2\)\]:tabular-nums/);
    });

    it("sets fixed widths on cols 2-5 so col 1 (workflow) takes the remaining space", () => {
      // Runs col is narrower (80px / w-20) than the cost cols (w-24).
      expect(result).toMatch(/\[&_th:nth-child\(2\)\]:w-20/);
      expect(result).toMatch(/\[&_th:nth-child\(3\)\]:w-24/);
      expect(result).toMatch(/\[&_th:nth-child\(4\)\]:w-24/);
      expect(result).toMatch(/\[&_th:nth-child\(5\)\]:w-24/);
    });

    it("bolds the Total Cost cell (col 5) so it reads as the bottom-line number", () => {
      expect(result).toMatch(/\[&_td:nth-child\(5\)\]:font-semibold/);
    });
  });
});
