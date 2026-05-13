import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { DashboardLoadState } from "./dashboard-load-state";
import { isDashboardLoadStateActive } from "@/util/dashboardLoadStateActive";

describe("DashboardLoadState", () => {
  it("returns null when data is present (no loading/error/empty)", () => {
    const html = renderToStaticMarkup(
      <DashboardLoadState
        isLoading={false}
        isError={false}
        isEmpty={false}
        surface="runs chart"
        emptyCopy="No runs to chart for this period."
      />,
    );
    expect(html).toBe("");
  });

  it("renders the loading skeleton when isLoading", () => {
    const html = renderToStaticMarkup(
      <DashboardLoadState
        isLoading
        isError={false}
        isEmpty={false}
        surface="runs chart"
        emptyCopy="No runs to chart for this period."
      />,
    );
    expect(html).toContain('data-state="loading"');
    expect(html).toContain("animate-pulse");
  });

  it("uses the supplied skeleton override when provided", () => {
    const html = renderToStaticMarkup(
      <DashboardLoadState
        isLoading
        isError={false}
        isEmpty={false}
        surface="runs chart"
        emptyCopy="No runs to chart for this period."
        skeleton={<span data-testid="custom-skel">custom</span>}
      />,
    );
    expect(html).toContain('data-testid="custom-skel"');
    expect(html).toContain("custom");
  });

  it("renders the empty placeholder with the exact emptyCopy string", () => {
    const html = renderToStaticMarkup(
      <DashboardLoadState
        isLoading={false}
        isError={false}
        isEmpty
        surface="runs chart"
        emptyCopy="No runs to chart for this period."
      />,
    );
    expect(html).toContain('data-state="empty"');
    expect(html).toContain("No runs to chart for this period.");
  });

  it("renders the error alert with surface in the title and message in the description (no duplication)", () => {
    const html = renderToStaticMarkup(
      <DashboardLoadState
        isLoading={false}
        isError
        isEmpty={false}
        error={new Error("backend exploded")}
        surface="runs chart"
        emptyCopy="No runs to chart for this period."
      />,
    );
    expect(html).toContain('data-state="error"');
    expect(html).toContain("Couldn&#x27;t load runs chart");
    expect(html).toContain("backend exploded");
    const matches = html.match(/Couldn&#x27;t load runs chart/g) ?? [];
    expect(matches.length).toBe(1);
  });

  it("falls back to a generic 'Try again.' description when error has no message", () => {
    const html = renderToStaticMarkup(
      <DashboardLoadState
        isLoading={false}
        isError
        isEmpty={false}
        surface="runs chart"
        emptyCopy="No runs to chart for this period."
      />,
    );
    expect(html).toContain("Try again.");
    const matches = html.match(/Couldn&#x27;t load runs chart/g) ?? [];
    expect(matches.length).toBe(1);
  });

  it("loading wins over error when both are true (mid-refetch)", () => {
    const html = renderToStaticMarkup(
      <DashboardLoadState
        isLoading
        isError
        isEmpty={false}
        surface="runs chart"
        emptyCopy="No runs to chart for this period."
      />,
    );
    expect(html).toContain('data-state="loading"');
    expect(html).not.toContain('data-state="error"');
  });

  it("renders the retry button only when a retry callback is provided and namespaces its testId", () => {
    const withRetry = renderToStaticMarkup(
      <DashboardLoadState
        isLoading={false}
        isError
        isEmpty={false}
        error={new Error("nope")}
        retry={() => undefined}
        surface="runs chart"
        emptyCopy="No runs to chart for this period."
        testId="runs-chart-load-state"
      />,
    );
    expect(withRetry).toContain('data-testid="runs-chart-load-state-retry"');
    expect(withRetry).toContain("Try again");

    const withoutRetry = renderToStaticMarkup(
      <DashboardLoadState
        isLoading={false}
        isError
        isEmpty={false}
        surface="runs chart"
        emptyCopy="No runs to chart for this period."
      />,
    );
    expect(withoutRetry).not.toContain("-retry");
  });

  it("falls back to dashboard-load-state-retry when no testId is provided", () => {
    const html = renderToStaticMarkup(
      <DashboardLoadState
        isLoading={false}
        isError
        isEmpty={false}
        retry={() => undefined}
        surface="runs chart"
        emptyCopy="No runs to chart for this period."
      />,
    );
    expect(html).toContain('data-testid="dashboard-load-state-retry"');
  });
});

describe("isDashboardLoadStateActive", () => {
  it("returns false when none of loading/error/empty applies", () => {
    expect(
      isDashboardLoadStateActive({
        isLoading: false,
        isError: false,
        isEmpty: false,
      }),
    ).toBe(false);
  });

  it("returns true when any one of loading/error/empty applies", () => {
    expect(
      isDashboardLoadStateActive({
        isLoading: true,
        isError: false,
        isEmpty: false,
      }),
    ).toBe(true);
    expect(
      isDashboardLoadStateActive({
        isLoading: false,
        isError: true,
        isEmpty: false,
      }),
    ).toBe(true);
    expect(
      isDashboardLoadStateActive({
        isLoading: false,
        isError: false,
        isEmpty: true,
      }),
    ).toBe(true);
  });
});
