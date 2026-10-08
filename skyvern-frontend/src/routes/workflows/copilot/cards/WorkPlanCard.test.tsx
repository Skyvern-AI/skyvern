// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { WorkPlanCard } from "./WorkPlanCard";

afterEach(cleanup);

describe("model-owned work plan", () => {
  it("renders each item exactly as the model wrote it, in order", () => {
    const items = [
      "continue past result selection",
      "reach the payment step",
      "return output.confirmation_code",
    ];
    render(<WorkPlanCard items={items} />);
    expect(
      screen.getAllByRole("listitem").map((entry) => entry.textContent),
    ).toEqual(items.map((item, index) => `${index + 1}${item}`));
  });

  it("marks what a revision added and removed, and folds once it is no longer current", () => {
    const { rerender } = render(
      <WorkPlanCard
        items={["scout the search page", "pay with the saved card"]}
        previous={["scout the search page", "reach the payment step"]}
      />,
    );
    expect(screen.getByText("Plan updated")).toBeTruthy();
    expect(screen.getByText(/1 new · 1 removed/)).toBeTruthy();
    const added = screen.getByText("pay with the saved card").closest("li")!;
    expect(added.textContent).toContain("New");
    expect(screen.getByText("reach the payment step").className).toContain(
      "line-through",
    );

    rerender(
      <WorkPlanCard
        items={["scout the search page", "pay with the saved card"]}
        previous={["scout the search page", "reach the payment step"]}
        current={false}
      />,
    );
    expect(screen.queryByText("pay with the saved card")).toBeNull();
    fireEvent.click(screen.getByRole("button", { expanded: false }));
    expect(screen.getByText("pay with the saved card")).toBeTruthy();
  });
});
