// @vitest-environment jsdom

import { fireEvent, render, screen } from "@testing-library/react";
import { useForm } from "react-hook-form";
import { describe, expect, test, vi } from "vitest";

import { Form, FormField } from "@/components/ui/form";

import { CredentialParameter } from "../types/workflowTypes";
import { RotatingCredentialField } from "./RotatingCredentialField";

const credentialNamesById = new Map([
  ["cred_1", "Cred One"],
  ["cred_2", "Cred Two"],
]);

function buildCredentialParameter(): CredentialParameter {
  return {
    credential_ids: ["cred_1", "cred_2"],
    selection_strategy: "round_robin",
    credential_id: "cred_1",
    key: "Portal_Login",
    parameter_type: "credential",
    workflow_id: "workflow_1",
    credential_parameter_id: "credential_parameter_1",
    description: null,
    created_at: "2026-07-13T00:00:00Z",
    modified_at: "2026-07-13T00:00:00Z",
    deleted_at: null,
  };
}

// field.onChange(undefined) is a no-op in react-hook-form, so drive value
// through the real RHF field (not useState) to exercise the actual clear path.
function ControlledRotatingCredentialField({
  initialValue,
  onChange,
}: {
  initialValue: string | null;
  onChange: (value: string | null) => void;
}) {
  const parameter = buildCredentialParameter();
  const form = useForm<Record<string, unknown>>({
    defaultValues: { [parameter.key]: initialValue },
  });

  return (
    <Form {...form}>
      <FormField
        control={form.control}
        name={parameter.key}
        render={({ field }) => (
          <RotatingCredentialField
            parameter={parameter}
            value={field.value}
            onChange={(nextValue) => {
              onChange(nextValue);
              field.onChange(nextValue);
            }}
            credentialNamesById={credentialNamesById}
            title="Portal Login"
            description="Rotating login credentials"
          />
        )}
      />
    </Form>
  );
}

describe("RotatingCredentialField", () => {
  test("can switch from a seeded force selection to configured rotation", () => {
    const onChange = vi.fn();

    render(
      <ControlledRotatingCredentialField
        initialValue="cred_1"
        onChange={onChange}
      />,
    );

    let radios = screen.getAllByRole("radio");
    expect(radios[0]?.getAttribute("aria-checked")).toBe("false");
    expect(radios[1]?.getAttribute("aria-checked")).toBe("true");

    fireEvent.click(screen.getByText("Use configured rotation"));

    expect(onChange).toHaveBeenCalledWith(null);
    radios = screen.getAllByRole("radio");
    expect(radios[0]?.getAttribute("aria-checked")).toBe("true");
    expect(radios[1]?.getAttribute("aria-checked")).toBe("false");
  });

  test("can force a single credential from rotation", () => {
    const onChange = vi.fn();

    render(
      <ControlledRotatingCredentialField
        initialValue={null}
        onChange={onChange}
      />,
    );

    let radios = screen.getAllByRole("radio");
    expect(radios[0]?.getAttribute("aria-checked")).toBe("true");
    expect(radios[1]?.getAttribute("aria-checked")).toBe("false");

    fireEvent.click(screen.getByText("Force one credential for this run"));

    expect(onChange).toHaveBeenCalledWith("cred_1");
    radios = screen.getAllByRole("radio");
    expect(radios[0]?.getAttribute("aria-checked")).toBe("false");
    expect(radios[1]?.getAttribute("aria-checked")).toBe("true");
  });

  test("defaults to rotation when value is empty", () => {
    render(
      <ControlledRotatingCredentialField
        initialValue={null}
        onChange={vi.fn()}
      />,
    );

    const radios = screen.getAllByRole("radio");
    expect(radios[0]?.getAttribute("aria-checked")).toBe("true");
    expect(radios[1]?.getAttribute("aria-checked")).toBe("false");
  });
});
