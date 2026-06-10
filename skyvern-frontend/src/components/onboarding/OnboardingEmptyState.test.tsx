import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { OnboardingEmptyState } from "./OnboardingEmptyState";

vi.mock("posthog-js", () => ({ default: { capture: vi.fn() } }));

describe("OnboardingEmptyState", () => {
  it("renders the title, description, and primary action", () => {
    const html = renderToStaticMarkup(
      <OnboardingEmptyState
        surface="runs"
        icon={<span data-testid="icon">icon</span>}
        title="Your run history will appear here"
        description="Create a workflow to get started."
        primaryAction={{ label: "Create workflow", onClick: vi.fn() }}
      />,
    );
    expect(html).toContain('data-testid="onboarding-empty-state-runs"');
    expect(html).toContain("Your run history will appear here");
    expect(html).toContain("Create a workflow to get started.");
    expect(html).toContain("Create workflow");
    expect(html).toContain('data-testid="icon"');
  });

  it("renders the secondary action when provided", () => {
    const html = renderToStaticMarkup(
      <OnboardingEmptyState
        surface="dashboard"
        icon={<span>icon</span>}
        title="Title"
        description="Desc"
        primaryAction={{ label: "Primary", onClick: vi.fn() }}
        secondaryAction={{ label: "Secondary", onClick: vi.fn() }}
      />,
    );
    expect(html).toContain("Primary");
    expect(html).toContain("Secondary");
  });

  it("omits the secondary action when not provided", () => {
    const html = renderToStaticMarkup(
      <OnboardingEmptyState
        surface="runs"
        icon={<span>icon</span>}
        title="Title"
        description="Desc"
        primaryAction={{ label: "Primary", onClick: vi.fn() }}
      />,
    );
    expect(html).toContain("Primary");
    const buttonCount = (html.match(/<button/g) ?? []).length;
    expect(buttonCount).toBe(1);
  });
});
