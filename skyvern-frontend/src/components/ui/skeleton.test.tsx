import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { Skeleton } from "./skeleton";

// Existing-caller preservation gate: the 3 specialized callsites
// (TaskListSkeletonRows, RunningTaskSkeleton, WorkflowRunOverviewSkeleton)
// pass `<Skeleton className="..." />` with no variant prop. The rendered
// HTML must keep the legacy single-<div> shape and the legacy core classes.
describe("Skeleton — existing-caller preservation", () => {
  it("renders a single div carrying the className passthrough", () => {
    const html = renderToStaticMarkup(<Skeleton className="h-4 w-full" />);
    expect(html).toMatch(/^<div\b/);
    expect(html).toContain("h-4");
    expect(html).toContain("w-full");
    // Legacy core classes — the rect default must keep emitting these.
    expect(html).toContain("animate-pulse");
    expect(html).toContain("rounded-md");
  });

  // PRESERVATION HARD GATE — no-prop default must render byte-identical HTML
  // to the pre-cva Skeleton. Cascade rebase relies on this; if this test
  // breaks, downstream PRs that style around <Skeleton> may surface visual
  // regressions. Don't relax this assertion without sc-67 sign-off.
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
    // Inline width/height in pixels so callers can size without className.
    expect(html).toContain("width:32px");
    expect(html).toContain("height:32px");
  });
});

describe("Skeleton — variant=text", () => {
  it("renders N stacked line divs (lines prop)", () => {
    const html = renderToStaticMarkup(<Skeleton variant="text" lines={3} />);
    // Three internal line bars; outer container counts as the wrapper.
    const lineMatches = html.match(/<div[^>]*data-skeleton-line/g) ?? [];
    expect(lineMatches.length).toBe(3);
  });

  it("defaults to a single line when lines is omitted", () => {
    const html = renderToStaticMarkup(<Skeleton variant="text" />);
    const lineMatches = html.match(/<div[^>]*data-skeleton-line/g) ?? [];
    expect(lineMatches.length).toBe(1);
  });
});
