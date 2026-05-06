import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { Input } from "./input";

// Contract: <Input> must render with the design-system tokens that the cloud
// app (and OSS app) configure via CSS vars. Locking the className contract
// here so a future shadcn-style refactor can't silently drop the token wiring.
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
    // Single input, no extra wrapping div / span.
    expect(html.match(/<input\b/g)?.length ?? 0).toBe(1);
    expect(html).toContain('placeholder="Search"');
  });
});
