// @vitest-environment jsdom

import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CopilotMarkdown } from "./CopilotMarkdown";

const TABLE = `| # | Role | Pay |
|---|------|-----|
| 1 | Staff Data Scientist |  |
| 2 | Senior ML Engineer | $190k |`;

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("CopilotMarkdown tables", () => {
  it("frames a table and sizes its readable floor from the column count", () => {
    const { container } = render(<CopilotMarkdown text={TABLE} />);

    const sizer = container.querySelector("table")!.parentElement!;
    expect(sizer.style.maxWidth).toBe("max(100%, 24rem)");
    expect(sizer.parentElement!.className).toContain("overflow-x-auto");
  });

  it("keeps every row's columns while the reply streams in", () => {
    const length = TABLE.length;
    for (let shown = 1; shown <= length; shown += 1) {
      const { container, unmount } = render(
        <CopilotMarkdown
          text={TABLE}
          reveal={{
            shown,
            gradientStart: Math.max(0, shown - 8),
            onCharacterCount: () => {},
          }}
        />,
      );
      const table = container.querySelector("table");
      if (table) {
        expect(table.querySelectorAll("tr > :not(th, td)")).toHaveLength(0);
        // The empty Pay cell of row 1 stays in place once row 2 has begun.
        const rows = table.querySelectorAll("tbody tr");
        if (rows.length === 2) expect(rows[0]!.children).toHaveLength(3);
      }
      unmount();
    }
  });

  it("fades the right edge only while there is more of the table to scroll to", () => {
    // jsdom has no layout: a 300px frame holding a 500px table.
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(300);
    vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockReturnValue(500);
    const { container } = render(<CopilotMarkdown text={TABLE} />);
    const frame =
      container.querySelector("table")!.parentElement!.parentElement!;
    expect(frame.className).toContain("mask-image");

    frame.scrollLeft = 200;
    fireEvent.scroll(frame);
    expect(frame.className).not.toContain("mask-image");
  });
});
