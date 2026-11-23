// @vitest-environment jsdom

import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, test, vi } from "vitest";

vi.mock("@/components/WorkflowBlockInputTextarea", () => ({
  WorkflowBlockInputTextarea: ({
    name,
    value,
    onChange,
  }: {
    name: string;
    value: string;
    onChange: (value: string) => void;
  }) => (
    <textarea
      aria-label={name}
      value={value}
      onChange={(event) => onChange(event.target.value)}
    />
  ),
}));

import { InputField } from "./IntegrationActionNode";

function Harness({ initial }: { initial: unknown }) {
  const [value, setValue] = useState<unknown>(initial);
  return (
    <>
      <InputField
        nodeId="n1"
        name="amount"
        schema={{ type: "number" }}
        value={value}
        disabled={false}
        onChange={setValue}
      />
      <output data-testid="stored">{JSON.stringify(value)}</output>
      <button onClick={() => setValue(42)}>set elsewhere</button>
    </>
  );
}

describe("integration action input fields", () => {
  test("a dropdown stays a dropdown unless its value is a template", () => {
    const field = (value: unknown) => (
      <InputField
        nodeId="n1"
        name="mode"
        schema={{ type: "string", enum: ["append", "update"] }}
        value={value}
        disabled={false}
        onChange={() => {}}
      />
    );
    const { rerender } = render(field("append"));
    expect(screen.getByRole("combobox")).toBeTruthy();

    rerender(field("{{ mode }}"));
    expect(screen.queryByRole("combobox")).toBeNull();
    expect((screen.getByLabelText("mode") as HTMLTextAreaElement).value).toBe(
      "{{ mode }}",
    );
  });

  test("keeps partial decimals while storing the number, and shows outside edits", () => {
    render(<Harness initial={undefined} />);
    const field = screen.getByLabelText("amount") as HTMLTextAreaElement;

    fireEvent.change(field, { target: { value: "1." } });
    expect(field.value).toBe("1.");
    fireEvent.change(field, { target: { value: "1.50" } });
    expect(field.value).toBe("1.50");
    expect(screen.getByTestId("stored").textContent).toBe("1.5");

    fireEvent.click(screen.getByText("set elsewhere"));
    expect(field.value).toBe("42");

    fireEvent.change(field, { target: { value: "{{" } });
    fireEvent.change(field, { target: { value: "{{ amount }}" } });
    expect(screen.getByLabelText("amount")).toBe(field);
    expect(field.value).toBe("{{ amount }}");
    expect(screen.getByTestId("stored").textContent).toBe('"{{ amount }}"');
  });
});
