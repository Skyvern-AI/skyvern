import { describe, expect, it } from "vitest";
import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

// Lock the --input / --ring CSS-var contract that the Input component, the
// shadcn dialog/popover/select primitives, and any future ring-using
// surface depend on. Without these vars, focus rings and input borders
// fall back to browser defaults and the dashboard looks unstyled.
//
// The 3 index.css files (cloud / eval / src) are separate entrypoints —
// each app loads exactly one. Asserting all three define the vars in both
// :root and .dark prevents a regression where one entrypoint silently
// drops a token that another entrypoint still ships.

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(HERE, "../../../..");
// Gate by directory, not file: a deleted index.css inside an existing
// entrypoint dir is the regression we want to fail loudly on.
function entrypoint(dir: string): string | null {
  if (!existsSync(resolve(REPO_ROOT, dir))) {
    return null;
  }
  return `${dir}/index.css`;
}
const CSS_FILES = [
  "skyvern-frontend/src",
  "skyvern-frontend/cloud",
  "skyvern-frontend/eval",
]
  .map(entrypoint)
  .filter((f): f is string => f !== null);

function load(file: string): string {
  return readFileSync(resolve(REPO_ROOT, file), "utf-8");
}

function blockBetween(css: string, openSelector: string): string {
  const idx = css.indexOf(openSelector);
  if (idx === -1) {
    return "";
  }
  const open = css.indexOf("{", idx);
  if (open === -1) {
    return "";
  }
  let depth = 1;
  for (let i = open + 1; i < css.length; i++) {
    if (css[i] === "{") depth++;
    else if (css[i] === "}") {
      depth--;
      if (depth === 0) return css.slice(open + 1, i);
    }
  }
  return "";
}

describe.each(CSS_FILES)("%s defines DS token vars", (file) => {
  const css = load(file);
  const root = blockBetween(css, ":root");
  const dark = blockBetween(css, ".dark");

  it("defines --input under :root", () => {
    expect(root).toMatch(/--input:\s*[^;]+;/);
  });

  it("defines --ring under :root", () => {
    expect(root).toMatch(/--ring:\s*[^;]+;/);
  });

  it("defines --input under .dark", () => {
    expect(dark).toMatch(/--input:\s*[^;]+;/);
  });

  it("defines --ring under .dark", () => {
    expect(dark).toMatch(/--ring:\s*[^;]+;/);
  });

  it("defines --success under :root", () => {
    expect(root).toMatch(/--success:\s*[^;]+;/);
  });

  it("defines --success-foreground under :root", () => {
    expect(root).toMatch(/--success-foreground:\s*[^;]+;/);
  });

  it("defines --warning under :root", () => {
    expect(root).toMatch(/--warning:\s*[^;]+;/);
  });

  it("defines --warning-foreground under :root", () => {
    expect(root).toMatch(/--warning-foreground:\s*[^;]+;/);
  });

  it("defines --tertiary under :root", () => {
    expect(root).toMatch(/--tertiary:\s*[^;]+;/);
  });

  it("defines --tertiary-foreground under :root", () => {
    expect(root).toMatch(/--tertiary-foreground:\s*[^;]+;/);
  });

  it("defines --cta under :root", () => {
    expect(root).toMatch(/--cta:\s*[^;]+;/);
  });

  it("defines --cta-hover under :root", () => {
    expect(root).toMatch(/--cta-hover:\s*[^;]+;/);
  });

  it("defines --cta-foreground under :root", () => {
    expect(root).toMatch(/--cta-foreground:\s*[^;]+;/);
  });

  it("defines --success under .dark", () => {
    expect(dark).toMatch(/--success:\s*[^;]+;/);
  });

  it("defines --warning under .dark", () => {
    expect(dark).toMatch(/--warning:\s*[^;]+;/);
  });

  it("defines --tertiary under .dark", () => {
    expect(dark).toMatch(/--tertiary:\s*[^;]+;/);
  });

  it("defines --tertiary-foreground under .dark", () => {
    expect(dark).toMatch(/--tertiary-foreground:\s*[^;]+;/);
  });

  it("defines --cta under .dark", () => {
    expect(dark).toMatch(/--cta:\s*[^;]+;/);
  });

  it("defines --cta-hover under .dark", () => {
    expect(dark).toMatch(/--cta-hover:\s*[^;]+;/);
  });

  it("defines --cta-foreground under .dark", () => {
    expect(dark).toMatch(/--cta-foreground:\s*[^;]+;/);
  });
});

describe("shared semantic tokens are consistent across cloud/eval/src", () => {
  function tokenValue(css: string, selector: string, token: string): string {
    const block = blockBetween(css, selector);
    const m = block.match(new RegExp(`${token}:\\s*([^;]+);`));
    return (m?.[1] ?? "").trim();
  }

  function expectConsistentToken(token: string, selector: string) {
    const values = CSS_FILES.map((f) => tokenValue(load(f), selector, token));
    expect(new Set(values).size).toBe(1);
  }

  it("light-mode --ring is the same value in all 3 entrypoints", () => {
    expectConsistentToken("--ring", ":root");
  });

  it("dark-mode --ring is the same value in all 3 entrypoints", () => {
    expectConsistentToken("--ring", ".dark");
  });

  it("light-mode --cta is the same value in all 3 entrypoints", () => {
    expectConsistentToken("--cta", ":root");
  });

  it("light-mode --cta-hover is the same value in all 3 entrypoints", () => {
    expectConsistentToken("--cta-hover", ":root");
  });

  it("dark-mode --cta is the same value in all 3 entrypoints", () => {
    expectConsistentToken("--cta", ".dark");
  });

  it("dark-mode --cta-hover is the same value in all 3 entrypoints", () => {
    expectConsistentToken("--cta-hover", ".dark");
  });
});

// The primary CTA must track the DS --primary token, not an independent
// brand value. Locks SKY-10608: --cta aliases --primary so the neutral
// primary is the single source of truth and #606bd2 cannot drift back.
describe("--cta tracks the DS --primary (SKY-10608)", () => {
  function tokenValue(css: string, selector: string, token: string): string {
    const block = blockBetween(css, selector);
    const m = block.match(new RegExp(`${token}:\\s*([^;]+);`));
    return (m?.[1] ?? "").trim();
  }

  describe.each(CSS_FILES)("%s", (file) => {
    const css = load(file);

    it("light-mode --cta equals --primary", () => {
      expect(tokenValue(css, ":root", "--cta")).toBe(
        tokenValue(css, ":root", "--primary"),
      );
    });

    it("dark-mode --cta equals --primary", () => {
      expect(tokenValue(css, ".dark", "--cta")).toBe(
        tokenValue(css, ".dark", "--primary"),
      );
    });

    it("light-mode --cta-foreground equals --primary-foreground", () => {
      expect(tokenValue(css, ":root", "--cta-foreground")).toBe(
        tokenValue(css, ":root", "--primary-foreground"),
      );
    });

    it("dark-mode --cta-foreground equals --primary-foreground", () => {
      expect(tokenValue(css, ".dark", "--cta-foreground")).toBe(
        tokenValue(css, ".dark", "--primary-foreground"),
      );
    });
  });
});
