import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { N8nIcon } from "./N8nIcon";

describe("N8nIcon", () => {
  it("renders the official n8n pink workflow mark without the old placeholder tile", () => {
    const { container } = render(<N8nIcon className="size-6" />);

    const svg = container.querySelector("svg");
    expect(svg?.getAttribute("viewBox")).toBe("0 0 304 160");
    expect(svg?.querySelector('path[fill="#EA4B71"]')).toBeTruthy();
    expect(svg?.querySelector('rect[fill="#101330"]')).toBeNull();
  });
});
