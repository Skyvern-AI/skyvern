// @vitest-environment jsdom

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ColumnMappingEditor } from "./ColumnMappingEditor";

describe("ColumnMappingEditor", () => {
  it("renders combined labels as option values without text content", () => {
    render(
      <ColumnMappingEditor
        idScope="test"
        value='{"myfield":"Z"}'
        onChange={vi.fn()}
        headers={[
          { letter: "A", name: "Name" },
          { letter: "B", name: "  " },
        ]}
      />,
    );

    const options = Array.from(document.querySelectorAll("option"));
    expect(
      options.map((option) => ({
        value: option.value,
        textContent: option.textContent,
      })),
    ).toEqual([
      { value: "A - Name", textContent: "" },
      { value: "B", textContent: "" },
    ]);
  });

  it("serializes a selected combined label as its bare letter", () => {
    const onChange = vi.fn();
    render(
      <ColumnMappingEditor
        idScope="test"
        value='{"myfield":"Z"}'
        onChange={onChange}
        headers={[{ letter: "A", name: "Name" }]}
      />,
    );

    fireEvent.change(screen.getByPlaceholderText("A - Name"), {
      target: { value: "A - Name" },
    });

    expect(onChange).toHaveBeenCalledWith('{"myfield":"A"}');
  });
});
