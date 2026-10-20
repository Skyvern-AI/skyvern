// @vitest-environment jsdom

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import type {
  CredentialApiResponse,
  OnePasswordItemsApiResponse,
} from "@/api/types";
import { getClient } from "@/api/AxiosClient";
import { useOnePasswordItemsQuery } from "@/routes/workflows/hooks/useOnePasswordItemsQuery";
import CloudContext from "@/store/CloudContext";
import { OnePasswordCredentialsList } from "./OnePasswordCredentialsList";
import { useImportedOnePasswordCredentialsQuery } from "./hooks/useImportedOnePasswordCredentialsQuery";

vi.mock("@/api/AxiosClient", () => ({ getClient: vi.fn() }));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));
vi.mock("@/routes/workflows/hooks/useOnePasswordItemsQuery", () => ({
  useOnePasswordItemsQuery: vi.fn(),
}));
vi.mock("./hooks/useImportedOnePasswordCredentialsQuery", () => ({
  useImportedOnePasswordCredentialsQuery: vi.fn(),
}));
vi.mock("./ImportOnePasswordDialog", () => ({
  ImportOnePasswordDialog: ({ trigger }: { trigger: ReactNode }) => trigger,
}));

const mockedUseOnePasswordItemsQuery = vi.mocked(useOnePasswordItemsQuery);
const mockedUseImportedCredentialsQuery = vi.mocked(
  useImportedOnePasswordCredentialsQuery,
);
const mockedGetClient = vi.mocked(getClient);

function mockCredentials(data: CredentialApiResponse[] = []) {
  mockedUseImportedCredentialsQuery.mockReturnValue({
    data,
    isLoading: false,
    isError: false,
  } as ReturnType<typeof useImportedOnePasswordCredentialsQuery>);
}

function mockItems(data: OnePasswordItemsApiResponse) {
  mockedUseOnePasswordItemsQuery.mockReturnValue({
    data,
    isLoading: false,
    isError: false,
  } as ReturnType<typeof useOnePasswordItemsQuery>);
  mockCredentials();
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

function importedCredential(
  index: number,
  folderId: string | null = null,
): CredentialApiResponse {
  return {
    credential_id: `credential-${index}`,
    credential_type: "password",
    name: `Credential ${index}`,
    vault_type: "onepassword",
    vault_id: "vault-1",
    item_id: `item-${index}`,
    folder_id: folderId,
    credential: {
      username: "user@example.com",
      totp_type: "none",
      totp_identifier: null,
    },
  };
}

function renderList(props: { search?: string } = {}, isCloud = false) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <CloudContext.Provider value={isCloud}>
        <MemoryRouter>
          <OnePasswordCredentialsList {...props} />
        </MemoryRouter>
      </CloudContext.Provider>
    </QueryClientProvider>,
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

  it("offers import from the connected-but-empty state when upstream items exist", () => {
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

    renderList();

    expect(
      screen.getByText(/no login or password items were returned/i),
    ).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Import from 1Password" }),
    ).toBeTruthy();
  });

  it("keeps import available for unimported non-password items", () => {
    mockItems({
      configured: true,
      source: "organization",
      items: [
        {
          item_id: "item-imported",
          title: "Imported card",
          vault_id: "vault-1",
          vault_name: "Example vault",
          category: "CREDIT_CARD",
        },
        {
          item_id: "item-new",
          title: "New card",
          vault_id: "vault-1",
          vault_name: "Example vault",
          category: "CREDIT_CARD",
        },
      ],
    });
    mockCredentials([{ ...importedCredential(1), item_id: "item-imported" }]);

    renderList();

    expect(
      screen.getByRole("button", { name: "Import from 1Password" }),
    ).toBeTruthy();
  });

  it("refreshes imported 1Password credentials from the section action", async () => {
    const post = vi.fn().mockResolvedValue({
      data: {
        refreshed: 2,
        stale: 1,
        skipped: 1,
        results: [
          {
            vault_id: "vault-1",
            item_id: "item-1",
            status: "refreshed",
          },
          {
            vault_id: "vault-1",
            item_id: "item-2",
            status: "stale",
          },
          {
            vault_id: "vault-1",
            item_id: "item-3",
            status: "skipped",
            reason: "Credential moved while refresh was in progress.",
          },
        ],
      },
    });
    mockedGetClient.mockResolvedValue({ post } as never);
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
    mockCredentials([
      {
        credential_id: "cred-1",
        credential_type: "password",
        name: "Example login",
        vault_type: "onepassword",
        vault_id: "vault-1",
        item_id: "item-1",
        credential: {
          username: "user@example.com",
          totp_type: "none",
          totp_identifier: null,
        },
      },
    ]);

    renderList();

    fireEvent.click(
      screen.getByRole("button", { name: "Refresh from 1Password" }),
    );

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/credentials/onepassword/refresh", {
        folder_id: null,
      }),
    );
    expect(
      await screen.findByText(/2 refreshed.*1 missing.*1 skipped/i),
    ).toBeTruthy();
  });

  it("refreshes more than 500 credentials with one request per folder", async () => {
    const post = vi.fn().mockResolvedValue({
      data: { refreshed: 250, stale: 0, skipped: 0, results: [] },
    });
    mockedGetClient.mockResolvedValue({ post } as never);
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
    mockCredentials([
      ...Array.from({ length: 250 }, (_, index) =>
        importedCredential(index, "folder-1"),
      ),
      ...Array.from({ length: 251 }, (_, index) =>
        importedCredential(index + 250),
      ),
    ]);

    renderList();
    fireEvent.click(
      screen.getByRole("button", { name: "Refresh from 1Password" }),
    );

    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(post).toHaveBeenNthCalledWith(
      1,
      "/credentials/onepassword/refresh",
      {
        folder_id: "folder-1",
      },
    );
    expect(post).toHaveBeenNthCalledWith(
      2,
      "/credentials/onepassword/refresh",
      {
        folder_id: null,
      },
    );
  });

  it("does not send a refresh batch larger than the endpoint limit", async () => {
    const post = vi.fn();
    mockedGetClient.mockResolvedValue({ post } as never);
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
    mockCredentials(
      Array.from({ length: 501 }, (_, index) =>
        importedCredential(index, "large-folder"),
      ),
    );

    renderList();
    fireEvent.click(
      screen.getByRole("button", { name: "Refresh from 1Password" }),
    );

    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toMatch(/more than 500/i),
    );
    expect(post).not.toHaveBeenCalled();
  });

  it("continues refreshing other folders after a folder returns 403", async () => {
    const post = vi
      .fn()
      .mockRejectedValueOnce({
        isAxiosError: true,
        response: { status: 403, data: { detail: "forbidden" } },
      })
      .mockResolvedValueOnce({
        data: {
          refreshed: 1,
          stale: 0,
          skipped: 0,
          results: [
            {
              vault_id: "vault-1",
              item_id: "item-2",
              status: "refreshed",
            },
          ],
        },
      });
    mockedGetClient.mockResolvedValue({ post } as never);
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
    mockCredentials([
      importedCredential(1, "restricted-folder"),
      importedCredential(2, "accessible-folder"),
    ]);

    renderList();
    fireEvent.click(
      screen.getByRole("button", { name: "Refresh from 1Password" }),
    );

    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(
      await screen.findByText(
        /1 refreshed.*some folders could not be refreshed/i,
      ),
    ).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toMatch(/access/i);
  });
});
