// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { OnboardingStepProgress } from "./OnboardingStepProgress";

afterEach(cleanup);

function expectDots(count: number, activeIndex: number) {
  const dots = screen.getByTestId("onboarding-step-dots").children;
  expect(dots).toHaveLength(count);
  expect(
    Array.from(dots).filter(
      (dot) => dot.getAttribute("data-active") === "true",
    ),
  ).toHaveLength(1);
  expect(dots[activeIndex]?.getAttribute("data-active")).toBe("true");
}

describe("OnboardingStepProgress", () => {
  it("renders two steps without a chip", () => {
    render(<OnboardingStepProgress stepIndex={1} stepCount={2} />);

    expectDots(2, 0);
    expect(screen.getByText("STEP 1 OF 2")).toBeTruthy();
    expect(screen.queryByText("OPTIONAL")).toBeNull();
    expect(screen.queryByText("FINAL STEP")).toBeNull();
  });

  it("renders one mutually exclusive chip across three steps", () => {
    const { rerender } = render(
      <OnboardingStepProgress stepIndex={2} stepCount={3} chip="optional" />,
    );

    expectDots(3, 1);
    expect(screen.getByText("STEP 2 OF 3")).toBeTruthy();
    expect(screen.getByText("OPTIONAL")).toBeTruthy();
    expect(screen.queryByText("FINAL STEP")).toBeNull();

    rerender(
      <OnboardingStepProgress stepIndex={3} stepCount={3} chip="final" />,
    );
    expectDots(3, 2);
    expect(screen.getByText("STEP 3 OF 3")).toBeTruthy();
    expect(screen.getByText("FINAL STEP")).toBeTruthy();
    expect(screen.queryByText("OPTIONAL")).toBeNull();
  });
});
