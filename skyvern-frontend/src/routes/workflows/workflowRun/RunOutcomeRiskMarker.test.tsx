// @vitest-environment jsdom

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { RunOutcomeRiskMarker } from "./RunOutcomeRiskMarker";

describe("RunOutcomeRiskMarker", () => {
  it("renders an amber marker when the run has outcome risk", () => {
    render(<RunOutcomeRiskMarker outcomeRisk={true} />);

    const marker = screen.getByLabelText("Completed with outcome risk");
    expect(marker).toBeDefined();
    expect(marker.className).toContain("text-warning");
  });

  it("renders nothing when the run has no outcome risk", () => {
    const { container } = render(<RunOutcomeRiskMarker outcomeRisk={false} />);

    expect(container.firstChild).toBeNull();
  });
});
