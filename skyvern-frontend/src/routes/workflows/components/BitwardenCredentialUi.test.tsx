// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BitwardenCredentialsList } from "@/routes/credentials/BitwardenCredentialsList";
import { useBitwardenItemsQuery } from "../hooks/useBitwardenItemsQuery";
import { BitwardenItemSelector } from "./BitwardenItemSelector";

vi.mock("@/components/ui/select", () => {
  const Pass = ({ children }: { children?: ReactNode }) => <>{children}</>;
  return {
    Select: Pass,
    SelectContent: Pass,
    SelectItemText: Pass,
    SelectTrigger: Pass,
    SelectValue: ({ placeholder }: { placeholder?: string }) => (
      <span>{placeholder}</span>
    ),
    CustomSelectItem: Pass,
  };
});

vi.mock("../hooks/useBitwardenItemsQuery", () => ({
  useBitwardenItemsQuery: vi.fn(),
}));

const mockedUseBitwardenItemsQuery = vi.mocked(useBitwardenItemsQuery);
const items = [
  {
    item_id: "password-item",
    title: "Acme Login",
    collection_id: "collection-1",
    credential_type: "password" as const,
    url: "https://app.acme.test",
  },
  {
    item_id: "card-item",
    title: "Acme Card",
    collection_id: "collection-2",
    credential_type: "credit_card" as const,
    url: null,
  },
  {
    item_id: "card-without-collection",
    title: "Personal Card",
    collection_id: null,
    credential_type: "credit_card" as const,
    url: null,
  },
];

function mockItems(configured = true, itemOverrides = items) {
  mockedUseBitwardenItemsQuery.mockReturnValue({
    data: { configured, items: itemOverrides },
    isLoading: false,
    isError: false,
  } as ReturnType<typeof useBitwardenItemsQuery>);
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("Bitwarden credential UI", () => {
  it("renders read-only password items on the credentials page", () => {
    mockItems();
    render(<BitwardenCredentialsList />);

    expect(screen.getByText("Bitwarden")).toBeTruthy();
    expect(screen.getByText("Acme Login")).toBeTruthy();
    expect(screen.getByText("app.acme.test")).toBeTruthy();
    expect(
      screen.getByText(
        "Credit cards and secrets are available in the workflow editor.",
      ),
    ).toBeTruthy();
    expect(screen.queryByText("Acme Card")).toBeNull();
  });

  it("shows the Bitwarden footnote when only non-password items are available", () => {
    mockItems(true, [items[1]!, items[2]!]);
    render(<BitwardenCredentialsList />);

    expect(screen.getByText("Bitwarden")).toBeTruthy();
    expect(
      screen.getByText(
        "Credit cards and secrets are available in the workflow editor.",
      ),
    ).toBeTruthy();
    expect(screen.queryByText("Acme Card")).toBeNull();
  });

  it("filters workflow-picker credit cards to items with collection IDs", () => {
    mockItems();
    render(
      <BitwardenItemSelector
        itemId=""
        credentialDataType="creditCard"
        onSelect={vi.fn()}
      />,
    );

    expect(screen.getByText("Acme Card")).toBeTruthy();
    expect(screen.queryByText("Acme Login")).toBeNull();
    expect(screen.queryByText("Personal Card")).toBeNull();
  });

  it("explains when credit cards exist but none are collection-scoped", () => {
    mockItems(true, [items[2]!]);
    render(
      <BitwardenItemSelector
        itemId=""
        credentialDataType="creditCard"
        onSelect={vi.fn()}
      />,
    );

    expect(
      screen.getByText("No collection-scoped Bitwarden credit cards found"),
    ).toBeTruthy();
    expect(screen.queryByText("Personal Card")).toBeNull();
  });
});
