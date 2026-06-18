// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/components/DropdownWithOptions", () => ({
  DropdownWithOptions: ({
    value,
    onChange,
  }: {
    value: string;
    onChange: (value: string) => void;
  }) => (
    <input
      aria-label="Brand"
      value={value}
      onChange={(event) => onChange(event.target.value)}
    />
  ),
}));

import {
  CreditCardCredentialContent,
  type CreditCardCredentialValues,
} from "./CreditCardCredentialContent";

const INITIAL_VALUES: CreditCardCredentialValues = {
  name: "card",
  cardNumber: "",
  cardExpirationDate: "",
  cardCode: "",
  cardBrand: "",
  cardHolderName: "",
  billingAddressLine1: "",
  billingAddressLine2: "",
  billingCity: "",
  billingState: "",
  billingStateCode: "",
  billingPostalCode: "",
  billingCountry: "",
  billingCountryCode: "",
  billingEmail: "",
  billingPhone: "",
  metadata: [{ key: "", value: "" }],
};

function Harness({
  onChangeSpy,
}: {
  onChangeSpy: (next: CreditCardCredentialValues) => void;
}) {
  const [values, setValues] =
    useState<CreditCardCredentialValues>(INITIAL_VALUES);

  return (
    <CreditCardCredentialContent
      values={values}
      onChange={(next) => {
        onChangeSpy(next);
        setValues(next);
      }}
    />
  );
}

describe("CreditCardCredentialContent", () => {
  afterEach(() => cleanup());

  it("collects optional metadata key-value rows", () => {
    const onChangeSpy = vi.fn();
    render(<Harness onChangeSpy={onChangeSpy} />);

    fireEvent.change(screen.getByLabelText("Metadata key 1"), {
      target: { value: "customer_id" },
    });
    fireEvent.change(screen.getByLabelText("Metadata value 1"), {
      target: { value: "cus_123" },
    });
    fireEvent.click(screen.getByText("Add"));
    fireEvent.change(screen.getByLabelText("Metadata key 2"), {
      target: { value: "checkout_profile" },
    });
    fireEvent.change(screen.getByLabelText("Metadata value 2"), {
      target: { value: "default" },
    });

    const lastCall = onChangeSpy.mock.calls[onChangeSpy.mock.calls.length - 1];
    expect(lastCall?.[0].metadata).toEqual([
      { key: "customer_id", value: "cus_123" },
      { key: "checkout_profile", value: "default" },
    ]);
  });
});
