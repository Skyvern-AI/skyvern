import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { Skeleton } from "./skeleton";

describe("Skeleton — existing-caller preservation", () => {
  it("renders a single div carrying the className passthrough", () => {
    const html = renderToStaticMarkup(<Skeleton className="h-4 w-full" />);
    expect(html).toMatch(/^<div\b/);
    expect(html).toContain("h-4");
    expect(html).toContain("w-full");
    expect(html).toContain("animate-pulse");
    expect(html).toContain("rounded-md");
  });

  it('matches `<Skeleton variant="rect" />` exactly when no variant prop is passed', () => {
    const noProp = renderToStaticMarkup(<Skeleton className="h-4 w-full" />);
    const explicit = renderToStaticMarkup(
      <Skeleton variant="rect" className="h-4 w-full" />,
    );
    expect(noProp).toBe(explicit);
  });
});

describe("Skeleton — variant=circle", () => {
  it("renders a round shape sized via inline style", () => {
    const html = renderToStaticMarkup(<Skeleton variant="circle" size={32} />);
    expect(html).toContain("rounded-full");
    expect(html).toContain("width:32px");
    expect(html).toContain("height:32px");
  });
});

describe("Skeleton — variant=text", () => {
  it("renders N stacked line divs (lines prop)", () => {
    const html = renderToStaticMarkup(<Skeleton variant="text" lines={3} />);
    const lineMatches = html.match(/<div[^>]*data-skeleton-line/g) ?? [];
    expect(lineMatches.length).toBe(3);
  });

  it("defaults to a single line when lines is omitted", () => {
    const html = renderToStaticMarkup(<Skeleton variant="text" />);
    const lineMatches = html.match(/<div[^>]*data-skeleton-line/g) ?? [];
    expect(lineMatches.length).toBe(1);
  });
});
