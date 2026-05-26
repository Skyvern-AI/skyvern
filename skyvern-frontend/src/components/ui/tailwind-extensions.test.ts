import { describe, expect, it } from "vitest";
// @ts-expect-error tailwind.config.js has no type declarations
import config from "../../../tailwind.config.js";

type TailwindConfigLike = {
  theme?: { extend?: { colors?: unknown; boxShadow?: unknown } };
};

describe("tailwind v2 brand + shadow extensions", () => {
  const cfg = config as TailwindConfigLike;
  const colors = (cfg.theme?.extend?.colors ?? {}) as Record<string, unknown>;
  const boxShadow = (cfg.theme?.extend?.boxShadow ?? {}) as Record<
    string,
    string
  >;

  it("registers brand color with DEFAULT/foreground/soft", () => {
    expect(colors.brand).toBeDefined();
    const brand = colors.brand as Record<string, string>;
    expect(brand.DEFAULT).toBe("hsl(var(--brand))");
    expect(brand.foreground).toBe("hsl(var(--brand-foreground))");
    expect(brand.soft).toBe("hsl(var(--brand-soft))");
  });

  it("registers shadow.sm from --shadow-sm", () => {
    expect(boxShadow.sm).toBe("var(--shadow-sm)");
  });

  it("registers shadow.card from --shadow-card", () => {
    expect(boxShadow.card).toBe("var(--shadow-card)");
  });

  it("registers shadow.popover from --shadow-popover", () => {
    expect(boxShadow.popover).toBe("var(--shadow-popover)");
  });
});
