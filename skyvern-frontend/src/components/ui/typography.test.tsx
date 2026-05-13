import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { TitleDescription } from "./typography";

describe("TitleDescription", () => {
  it("renders the title text inside a heading element", () => {
    const html = renderToStaticMarkup(
      <TitleDescription title="Usage & Operations" />,
    );
    expect(html).toContain("Usage &amp; Operations");
    expect(html).toMatch(/<h2\b/);
  });

  it("renders the description text inside a <p> when provided", () => {
    const html = renderToStaticMarkup(
      <TitleDescription
        title="Usage & Operations"
        description="Aggregate metrics for the period"
      />,
    );
    expect(html).toContain("Aggregate metrics for the period");
    expect(html).toMatch(/<p\b/);
  });

  it("omits the description <p> entirely when description is undefined", () => {
    const html = renderToStaticMarkup(
      <TitleDescription title="Just a title" />,
    );
    expect(html).not.toMatch(/<p\b/);
  });

  it("omits the description <p> when description is an empty string", () => {
    const html = renderToStaticMarkup(
      <TitleDescription title="Just a title" description="" />,
    );
    expect(html).not.toMatch(/<p\b/);
  });

  it("respects the `as` prop to override the default heading level", () => {
    const html = renderToStaticMarkup(
      <TitleDescription as="h3" title="Sub-section" />,
    );
    expect(html).toMatch(/<h3\b/);
    expect(html).not.toMatch(/<h2\b/);
  });

  it("forwards a className to the container so callers can space the block", () => {
    const html = renderToStaticMarkup(
      <TitleDescription title="Block" className="mb-6" />,
    );
    expect(html).toContain("mb-6");
  });
});
