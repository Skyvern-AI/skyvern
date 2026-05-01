// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

afterEach(() => cleanup());

vi.mock("../../../hooks/useCredentialsQuery", () => ({
  useCredentialsQuery: () => ({ data: [] }),
}));

vi.mock("@/components/WorkflowBlockInputTextarea", () => ({
  WorkflowBlockInputTextarea: (props: {
    value: string;
    onChange: (v: string) => void;
    placeholder?: string;
  }) => (
    <input
      data-testid="wb-input"
      value={props.value}
      placeholder={props.placeholder}
      onChange={(e) => props.onChange(e.target.value)}
    />
  ),
}));

vi.mock("../../../components/CredentialSelector", () => ({
  CredentialSelector: () => <div data-testid="cred-selector" />,
}));

import { PayloadParameterFields } from "./PayloadParameterFields";

describe("PayloadParameterFields", () => {
  it("renders only the placeholder when target has 0 params and payload is empty", () => {
    render(
      <PayloadParameterFields
        parameters={[]}
        payload="{}"
        onChange={vi.fn()}
        nodeId="n1"
        isLoading={false}
      />,
    );
    expect(screen.getByText(/no input parameters/i)).toBeTruthy();
    expect(screen.queryByText(/dormant payload entries/i)).not.toBeTruthy();
  });

  it("surfaces dormant entries when target has 0 params but payload has saved keys", () => {
    render(
      <PayloadParameterFields
        parameters={[]}
        payload={JSON.stringify({ file_url: "{{ x..y }}", text: "hi" })}
        onChange={vi.fn()}
        nodeId="n1"
        isLoading={false}
      />,
    );
    expect(screen.getByText(/dormant payload entries/i)).toBeTruthy();
    expect(screen.getByText("file_url")).toBeTruthy();
    expect(screen.getByText("{{ x..y }}")).toBeTruthy();
    expect(screen.getByText("text")).toBeTruthy();
    expect(screen.getByText("hi")).toBeTruthy();
  });

  it("delete-dormant-key button calls onChange without that key", () => {
    const onChange = vi.fn();
    render(
      <PayloadParameterFields
        parameters={[]}
        payload={JSON.stringify({ file_url: "x", text: "y" })}
        onChange={onChange}
        nodeId="n1"
        isLoading={false}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /delete file_url/i }));
    expect(onChange).toHaveBeenCalledWith(
      JSON.stringify({ text: "y" }, null, 2),
    );
  });
});
