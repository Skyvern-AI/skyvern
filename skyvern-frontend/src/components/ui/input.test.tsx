import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { Input } from "./input";

describe("Input — DS token contract", () => {
  it("renders with border-input so the --input CSS var drives the border color", () => {
    const html = renderToStaticMarkup(<Input />);
    expect(html).toContain("border-input");
  });

  it("wires the focus ring to --ring via focus-visible:ring-ring", () => {
    const html = renderToStaticMarkup(<Input />);
    expect(html).toContain("focus-visible:ring-ring");
  });

  it("renders a single <input> element so existing form-libraries keep working", () => {
    const html = renderToStaticMarkup(<Input placeholder="Search" />);
    expect(html.match(/<input\b/g)?.length ?? 0).toBe(1);
    expect(html).toContain('placeholder="Search"');
  });
});
