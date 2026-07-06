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

const REQUIRED_TOKEN_CASES = [
  ["--input", ":root"],
  ["--ring", ":root"],
  ["--input", ".dark"],
  ["--ring", ".dark"],
  ["--success", ":root"],
  ["--success-foreground", ":root"],
  ["--warning", ":root"],
  ["--warning-foreground", ":root"],
  ["--tertiary", ":root"],
  ["--tertiary-foreground", ":root"],
  ["--cta", ":root"],
  ["--cta-hover", ":root"],
  ["--cta-foreground", ":root"],
  ["--success", ".dark"],
  ["--warning", ".dark"],
  ["--tertiary", ".dark"],
  ["--tertiary-foreground", ".dark"],
  ["--cta", ".dark"],
  ["--cta-hover", ".dark"],
  ["--cta-foreground", ".dark"],
] as const;

describe.each(CSS_FILES)("%s defines DS token vars", (file) => {
  const css = load(file);
  const root = blockBetween(css, ":root");
  const dark = blockBetween(css, ".dark");

  it.each(REQUIRED_TOKEN_CASES)("defines %s under %s", (token, selector) => {
    const block = selector === ":root" ? root : dark;
    expect(block).toMatch(new RegExp(`${token}:\\s*[^;]+;`));
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

  it.each([
    ["light-mode --ring", "--ring", ":root"],
    ["dark-mode --ring", "--ring", ".dark"],
    ["light-mode --cta", "--cta", ":root"],
    ["light-mode --cta-hover", "--cta-hover", ":root"],
    ["dark-mode --cta", "--cta", ".dark"],
    ["dark-mode --cta-hover", "--cta-hover", ".dark"],
  ] as const)(
    "%s is the same value in all 3 entrypoints",
    (_, token, selector) => {
      expectConsistentToken(token, selector);
    },
  );
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

    it.each([
      ["light-mode --cta equals --primary", ":root", "--cta", "--primary"],
      ["dark-mode --cta equals --primary", ".dark", "--cta", "--primary"],
      [
        "light-mode --cta-foreground equals --primary-foreground",
        ":root",
        "--cta-foreground",
        "--primary-foreground",
      ],
      [
        "dark-mode --cta-foreground equals --primary-foreground",
        ".dark",
        "--cta-foreground",
        "--primary-foreground",
      ],
    ] as const)("%s", (_, selector, ctaToken, primaryToken) => {
      expect(tokenValue(css, selector, ctaToken)).toBe(
        tokenValue(css, selector, primaryToken),
      );
    });
  });
});
