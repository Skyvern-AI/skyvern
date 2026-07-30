import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TemplateExpressionRow } from "./TemplateExpressionRow";

afterEach(() => {
  cleanup();
});

describe("TemplateExpressionRow", () => {
  it("renders the template-expression action and handles clicks", () => {
    const onClick = vi.fn();
    render(<TemplateExpressionRow onClick={onClick} />);

    expect(screen.getByText("{{}}")).toBeTruthy();
    const button = screen
      .getByText("Use template expression")
      .closest("button");
    expect(button).not.toBeNull();

    fireEvent.click(button!);

    expect(onClick).toHaveBeenCalledOnce();
  });
});
