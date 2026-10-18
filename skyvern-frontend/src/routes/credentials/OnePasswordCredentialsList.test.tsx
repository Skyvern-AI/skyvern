// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { OnePasswordItemsApiResponse } from "@/api/types";
import { useOnePasswordItemsQuery } from "@/routes/workflows/hooks/useOnePasswordItemsQuery";
import CloudContext from "@/store/CloudContext";
import { OnePasswordCredentialsList } from "./OnePasswordCredentialsList";

vi.mock("@/routes/workflows/hooks/useOnePasswordItemsQuery", () => ({
  useOnePasswordItemsQuery: vi.fn(),
}));

const mockedUseOnePasswordItemsQuery = vi.mocked(useOnePasswordItemsQuery);

function mockItems(data: OnePasswordItemsApiResponse) {
  mockedUseOnePasswordItemsQuery.mockReturnValue({
    data,
    isLoading: false,
    isError: false,
  } as ReturnType<typeof useOnePasswordItemsQuery>);
}

function mockNonPasswordItems() {
  mockItems({
    configured: true,
    source: "organization",
    items: [
      {
        item_id: "item-1",
        title: "Example card",
        vault_id: "vault-1",
        vault_name: "Example vault",
        category: "CREDIT_CARD",
      },
    ],
  });
}

function renderList(props: { search?: string } = {}, isCloud = false) {
  return render(
    <CloudContext.Provider value={isCloud}>
      <MemoryRouter>
        <OnePasswordCredentialsList {...props} />
      </MemoryRouter>
    </CloudContext.Provider>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("OnePasswordCredentialsList empty states", () => {
  it.each([
    { build: "OSS", isCloud: false, href: "/settings" },
    {
      build: "Cloud",
      isCloud: true,
      href: "/integrations?query=1Password",
    },
  ])(
    "links to the setup surface in $build for a connected service account without listable password items",
    ({ isCloud, href }) => {
      mockNonPasswordItems();
      renderList({}, isCloud);

      expect(
        screen.getByText(
          /no login or password items were returned from readable 1Password vaults/i,
        ),
      ).toBeTruthy();
      expect(
        screen
          .getByRole("link", { name: "1Password integration settings" })
          .getAttribute("href"),
      ).toBe(href);
    },
  );

  it("explains that instance-default items need IDs in the login block", () => {
    mockItems({ configured: false, source: "instance_default", items: [] });

    renderList();

    expect(
      screen.getByText(
        /items cannot be listed here.*vault ID and item ID in the login block/i,
      ),
    ).toBeTruthy();
  });

  it("keeps the unconfigured state hidden", () => {
    mockItems({ configured: false, source: null, items: [] });

    renderList();

    expect(screen.queryByText("1Password")).toBeNull();
    expect(
      screen.queryByRole("link", { name: "1Password integration settings" }),
    ).toBeNull();
  });

  it("does not show the empty-state guidance when search hides existing items", () => {
    mockItems({
      configured: true,
      source: "organization",
      items: [
        {
          item_id: "item-1",
          title: "Example login",
          vault_id: "vault-1",
          vault_name: "Example vault",
          category: "LOGIN",
        },
      ],
    });

    renderList({ search: "no match" });

    expect(
      screen.queryByText(/no login or password items were returned/i),
    ).toBeNull();
    expect(
      screen.queryByRole("link", { name: "1Password integration settings" }),
    ).toBeNull();
  });
});
