// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { WorkPlanCard } from "./WorkPlanCard";

afterEach(cleanup);

describe("model-owned work plan", () => {
  it("renders each item exactly as the model wrote it", () => {
    const items = [
      "continue past result selection",
      "reach the payment step",
      "return output.confirmation_code",
    ];
    render(<WorkPlanCard items={items} />);
    expect(
      screen.getAllByRole("listitem").map((entry) => entry.textContent),
    ).toEqual(items.map((item, index) => `${index + 1}.${item}`));
  });
});
