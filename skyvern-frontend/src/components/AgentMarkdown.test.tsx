// @vitest-environment jsdom

import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { BlockMarkdown, InlineMarkdown } from "./AgentMarkdown";

afterEach(cleanup);

// Callers render agent prose inside a <button> and, for the timeline row, on one truncated line. A
// block element breaks the line box, and an anchor or form control nested in a button is invalid
// HTML the browser may reparent. Untrusted model prose has to land inside this set either way.
const FORBIDDEN_TAGS = [
  "a",
  "blockquote",
  "div",
  "form",
  "h1",
  "h2",
  "h3",
  "h4",
  "h5",
  "h6",
  "hr",
  "img",
  "input",
  "li",
  "ol",
  "p",
  "pre",
  "section",
  "table",
  "tbody",
  "td",
  "th",
  "thead",
  "tr",
  "ul",
];

const HOSTILE_INPUTS: Array<[string, string]> = [
  ["raw html anchor", '<a href="javascript:alert(1)">click</a> tail'],
  ["raw html img with handler", '<img src=x onerror="alert(1)"> tail'],
  ["raw script tag", "<script>alert(1)</script> tail"],
  ["raw div", "<div>block</div> tail"],
  ["markdown link to a javascript uri", "[click](javascript:alert(1)) tail"],
  ["reference link", "[click][r] tail\n\n[r]: javascript:alert(1)"],
  ["image", "![beacon](https://example.com/x.png) tail"],
  ["bare url", "see https://example.com/a?b=1 now"],
  ["table", "| a | b |\n| - | - |\n| 1 | 2 |"],
  ["task list", "- [ ] one\n- [x] two"],
  ["nested list", "- a\n  - b\n    - c"],
  ["fenced code block", "before\n```js\nconst x = 1;\n```\nafter"],
  ["thematic break", "a\n\n---\n\nb"],
  ["heading", "# Big heading\n\nbody"],
  ["blockquote", "> quoted\n\nafter"],
  ["footnote", "text[^1]\n\n[^1]: note"],
  ["multiple paragraphs", "line one\n\nline two"],
];

// The safety property is structural, not textual: escaped source legitimately contains the literal
// characters "javascript:" or "onerror=" as inert text, so assert that nothing live survived - no
// URL-bearing or event-handler attribute anywhere, and no unescaped script/iframe in the markup.
function expectInert(container: HTMLElement) {
  for (const node of Array.from(container.querySelectorAll("*"))) {
    for (const attribute of Array.from(node.attributes)) {
      expect(attribute.name).not.toMatch(/^on/i);
      expect([
        "href",
        "src",
        "srcset",
        "formaction",
        "xlink:href",
      ]).not.toContain(attribute.name);
    }
  }
  expect(container.innerHTML).not.toMatch(/<(?:script|iframe|object|embed)\b/i);
}

function tagsIn(container: HTMLElement): Array<string> {
  return [
    ...new Set(
      Array.from(container.querySelectorAll("*")).map((node) =>
        node.tagName.toLowerCase(),
      ),
    ),
  ];
}

describe("InlineMarkdown", () => {
  it("renders emphasis as bold rather than its source syntax", () => {
    const { container } = render(
      <InlineMarkdown text="**Navigating account details** then the table" />,
    );

    expect(container.querySelector("strong")?.textContent).toBe(
      "Navigating account details",
    );
    expect(container.textContent).not.toContain("**");
  });

  // react-markdown's skipHtml *deletes* raw HTML rather than escaping it, so reasoning that names a
  // tag would silently lose those words - and a whole paragraph, for a block-level tag.
  it.each([
    ["Clicking the <button> inside the <form> to submit"],
    ["Reasoning: use x<y and y>z"],
    ["<script>alert(1)</script> tail"],
  ])("keeps every word of %s", (source) => {
    const { container } = render(<InlineMarkdown text={source} />);

    expect(container.textContent).toBe(source);
  });

  it.each(HOSTILE_INPUTS)("keeps %s inline and inert", (_name, source) => {
    const { container } = render(<InlineMarkdown text={source} />);

    expect(tagsIn(container).filter((t) => FORBIDDEN_TAGS.includes(t))).toEqual(
      [],
    );
    expectInert(container);
  });

  // Prose that renders to nothing puts the row back to a bare icon and index - the bug this
  // renderer exists to fix - so no construct is allowed to disappear.
  it.each([
    ["image only", "![the invoices table](https://example.com/x.png)"],
    ["rule only", "---"],
    ["image and rule", "![a chart](https://example.com/c.png)\n\n---"],
    ["image with no alt", "![](https://example.com/x.png)"],
    ["bare reference definition", "[r]: https://example.com"],
    ["html comment only", "<!-- nothing to see -->"],
  ])("renders visible content for %s", (_name, source) => {
    const { container } = render(<InlineMarkdown text={source} />);

    expect(container.textContent?.trim()).not.toBe("");
  });

  it.each([
    ["image with no alt", "![](https://example.com/x.png)"],
    ["bare reference definition", "[r]: https://example.com"],
  ])(
    "shows the caller's fallback when %s renders to nothing",
    (_name, source) => {
      const { container } = render(
        <InlineMarkdown text={source} fallback={<span>Click</span>} />,
      );

      expect(container.textContent?.trim()).toBe("Click");
    },
  );

  it("does not use the fallback when the prose does render", () => {
    const { container } = render(
      <InlineMarkdown
        text="**Opened** the invoice"
        fallback={<span>Click</span>}
      />,
    );

    expect(container.textContent).toContain("Opened the invoice");
    expect(container.textContent).not.toContain("Click");
  });

  it("keeps an image's alt text, which is the only readable part of it", () => {
    const { container } = render(
      <InlineMarkdown text="![the invoices table](https://example.com/x.png)" />,
    );

    expect(container.textContent).toContain("the invoices table");
  });

  it("keeps a link's visible text without making it clickable", () => {
    const { container } = render(
      <InlineMarkdown text="[the invoices page](https://example.com)" />,
    );

    expect(container.textContent).toContain("the invoices page");
    expect(container.querySelector("a")).toBeNull();
  });
});

describe("BlockMarkdown", () => {
  it.each(HOSTILE_INPUTS)("keeps %s inert", (_name, source) => {
    const { container } = render(<BlockMarkdown text={source} />);

    expect(tagsIn(container).filter((t) => FORBIDDEN_TAGS.includes(t))).toEqual(
      [],
    );
    expectInert(container);
  });

  it("separates a heading from the paragraph after it", () => {
    const { container } = render(<BlockMarkdown text={"# Plan\n\nBody"} />);

    const blocks = Array.from(container.querySelectorAll("span")).filter(
      (node) => node.className.includes("block"),
    );
    expect(blocks.map((node) => node.textContent)).toEqual(["Plan", "Body"]);
  });

  it("separates paragraphs instead of running them together", () => {
    const { container } = render(<BlockMarkdown text={"first\n\nsecond"} />);

    const blocks = Array.from(container.querySelectorAll("span")).filter(
      (node) => node.className.includes("block"),
    );
    expect(blocks.map((node) => node.textContent)).toEqual(["first", "second"]);
  });
});
