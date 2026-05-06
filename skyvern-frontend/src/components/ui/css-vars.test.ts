import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

// Lock the --input / --ring CSS-var contract that the Input component, the
// shadcn dialog/popover/select primitives, and any future ring-using
// surface depend on. Without these vars, focus rings and input borders
// fall back to browser defaults and the dashboard looks unstyled.
//
// The 3 index.css files (cloud / eval / src) are separate entrypoints —
// each app loads exactly one. Asserting all three define the vars in both
// :root and .dark prevents a regression where one entrypoint silently
// drops a token that another entrypoint still ships.

const REPO_ROOT = resolve(__dirname, "../../../..");
const CSS_FILES = [
  "skyvern-frontend/src/index.css",
  "skyvern-frontend/cloud/index.css",
  "skyvern-frontend/eval/index.css",
];

function load(file: string): string {
  return readFileSync(resolve(REPO_ROOT, file), "utf-8");
}

function blockBetween(css: string, openSelector: string): string {
  // Naive but sufficient: find the selector, then capture from there to the
  // matching closing brace of its top-level block. The :root and .dark blocks
  // in our index.css files are flat (no nested rules), so this is safe.
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

  // Status tokens — consumed by Card tone variants and any future
  // status-tinted surface. Required in BOTH :root and .dark of all 3
  // entrypoints so border-success/border-warning render the same color
  // regardless of which app loads.
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

  it("defines --success under .dark", () => {
    expect(dark).toMatch(/--success:\s*[^;]+;/);
  });

  it("defines --warning under .dark", () => {
    expect(dark).toMatch(/--warning:\s*[^;]+;/);
  });
});

describe("--ring is consistent across cloud/eval/src", () => {
  // Sub-pixel drift (4.9% vs 5%) shipped a long time ago and is the kind
  // of polish gap a leadership walkthrough would notice. Lock the
  // harmonization in test so a future copy-paste into one entrypoint
  // can't silently re-introduce the drift.
  function ringValue(css: string, selector: string): string {
    const block = blockBetween(css, selector);
    const m = block.match(/--ring:\s*([^;]+);/);
    return (m?.[1] ?? "").trim();
  }

  it("light-mode --ring is the same value in all 3 entrypoints", () => {
    const values = CSS_FILES.map((f) => ringValue(load(f), ":root"));
    expect(new Set(values).size).toBe(1);
  });

  it("dark-mode --ring is the same value in all 3 entrypoints", () => {
    const values = CSS_FILES.map((f) => ringValue(load(f), ".dark"));
    expect(new Set(values).size).toBe(1);
  });
});
