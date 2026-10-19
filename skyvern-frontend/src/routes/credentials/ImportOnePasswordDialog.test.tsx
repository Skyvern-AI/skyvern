// @vitest-environment jsdom

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { getClient } from "@/api/AxiosClient";
import type {
  CredentialApiResponse,
  OnePasswordItemsApiResponse,
} from "@/api/types";
import { Button } from "@/components/ui/button";
import { ImportOnePasswordDialog } from "./ImportOnePasswordDialog";

vi.mock("@/api/AxiosClient", () => ({ getClient: vi.fn() }));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

const mockedGetClient = vi.mocked(getClient);

function makeImportedCredential(itemId: string): CredentialApiResponse {
  return {
    credential_id: `credential-${itemId}`,
    credential_type: "password",
    name: `Existing ${itemId}`,
    vault_type: "onepassword",
    vault_id: "vault-1",
    item_id: itemId,
    credential: {
      username: "user@example.com",
      totp_type: "none",
      totp_identifier: null,
    },
  };
}

function renderDialog() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <ImportOnePasswordDialog
        trigger={<Button>Import from 1Password</Button>}
      />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ImportOnePasswordDialog", () => {
  it("selects available items by vault and renders per-item import results", async () => {
    const items: OnePasswordItemsApiResponse = {
      configured: true,
      source: "organization",
      items: [
        {
          item_id: "item-existing",
          title: "Existing login",
          vault_id: "vault-1",
          vault_name: "Team vault",
          category: "LOGIN",
        },
        {
          item_id: "item-new",
          title: "New login",
          vault_id: "vault-1",
          vault_name: "Team vault",
          category: "LOGIN",
        },
        {
          item_id: "item-unsupported",
          title: "Unsupported item",
          vault_id: "vault-2",
          vault_name: "Archive vault",
          category: "DOCUMENT",
        },
      ],
    };
    const firstPage = Array.from({ length: 500 }, (_, index) =>
      makeImportedCredential(`unrelated-${index}`),
    );
    const get = vi.fn(
      async (path: string, options?: { params?: Record<string, unknown> }) => {
        if (path === "/credentials/onepassword/items") {
          return { data: items };
        }
        if (path === "/credentials") {
          return {
            data:
              options?.params?.page === 1
                ? firstPage
                : [makeImportedCredential("item-existing")],
          };
        }
        if (path === "/credential_folders") {
          return {
            data: [
              {
                folder_id: "folder-1",
                organization_id: "org-1",
                title: "Imported logins",
                description: null,
                credential_count: 0,
                created_at: "2026-10-18T00:00:00Z",
                modified_at: "2026-10-18T00:00:00Z",
              },
            ],
          };
        }
        throw new Error(`Unexpected GET ${path}`);
      },
    );
    const post = vi.fn().mockResolvedValue({
      data: {
        results: [
          {
            vault_id: "vault-1",
            item_id: "item-new",
            status: "imported",
            reason: null,
          },
          {
            vault_id: "vault-2",
            item_id: "item-unsupported",
            status: "skipped",
            reason: "Unsupported 1Password category: DOCUMENT",
          },
        ],
      },
    });
    mockedGetClient.mockResolvedValue({ get, post } as never);

    renderDialog();
    fireEvent.click(
      screen.getByRole("button", { name: "Import from 1Password" }),
    );

    const existingCheckbox = await screen.findByRole("checkbox", {
      name: "Select Existing login",
    });
    expect((existingCheckbox as HTMLButtonElement).disabled).toBe(true);
    expect(get).toHaveBeenCalledWith(
      "/credentials",
      expect.objectContaining({
        params: expect.objectContaining({ page: 2, page_size: 500 }),
      }),
    );

    fireEvent.click(
      screen.getByRole("checkbox", { name: "Select all in Team vault" }),
    );
    fireEvent.click(
      screen.getByRole("checkbox", { name: "Select Unsupported item" }),
    );
    expect(screen.getByRole("button", { name: "No folder" })).toBeTruthy();
    fireEvent.click(
      screen.getByRole("button", { name: "Import selected items" }),
    );

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/credentials/onepassword/import", {
        items: [
          { vault_id: "vault-1", item_id: "item-new" },
          { vault_id: "vault-2", item_id: "item-unsupported" },
        ],
      });
    });
    expect(await screen.findByText("Imported")).toBeTruthy();
    expect(screen.getByText("Skipped")).toBeTruthy();
    expect(
      screen.getByText("Unsupported 1Password category: DOCUMENT"),
    ).toBeTruthy();
    expect(screen.getAllByText("New login").length).toBeGreaterThan(1);
  });

  it("retries a forbidden batch item by item and imports the remaining items", async () => {
    const items: OnePasswordItemsApiResponse = {
      configured: true,
      source: "organization",
      items: [
        {
          item_id: "item-hidden",
          title: "Already linked item",
          vault_id: "vault-1",
          vault_name: "Team vault",
          category: "LOGIN",
        },
        {
          item_id: "item-new",
          title: "New login",
          vault_id: "vault-1",
          vault_name: "Team vault",
          category: "LOGIN",
        },
      ],
    };
    const get = vi.fn(async (path: string) => {
      if (path === "/credentials/onepassword/items") {
        return { data: items };
      }
      if (path === "/credentials") return { data: [] };
      throw new Error(`Unexpected GET ${path}`);
    });
    const forbidden = {
      isAxiosError: true,
      response: { status: 403, data: { detail: "forbidden" } },
    };
    const post = vi.fn(
      async (_path: string, request: { items: Array<{ item_id: string }> }) => {
        if (
          request.items.length > 1 ||
          request.items[0]?.item_id === "item-hidden"
        ) {
          throw forbidden;
        }
        return {
          data: {
            results: [
              {
                vault_id: "vault-1",
                item_id: "item-new",
                status: "imported",
                reason: null,
              },
            ],
          },
        };
      },
    );
    mockedGetClient.mockResolvedValue({ get, post } as never);

    renderDialog();
    fireEvent.click(
      screen.getByRole("button", { name: "Import from 1Password" }),
    );
    fireEvent.click(
      await screen.findByRole("checkbox", {
        name: "Select Already linked item",
      }),
    );
    fireEvent.click(screen.getByRole("checkbox", { name: "Select New login" }));
    fireEvent.click(
      screen.getByRole("button", { name: "Import selected items" }),
    );

    expect(await screen.findByText("Imported")).toBeTruthy();
    expect(screen.getByText("Not permitted")).toBeTruthy();
    expect(
      screen.queryByText(/already linked in a folder you can't access/i),
    ).toBeNull();
    expect(post).toHaveBeenCalledTimes(3);
    expect(post).toHaveBeenNthCalledWith(1, "/credentials/onepassword/import", {
      items: [
        { vault_id: "vault-1", item_id: "item-hidden" },
        { vault_id: "vault-1", item_id: "item-new" },
      ],
    });
    expect(post).toHaveBeenNthCalledWith(2, "/credentials/onepassword/import", {
      items: [{ vault_id: "vault-1", item_id: "item-hidden" }],
    });
    expect(post).toHaveBeenNthCalledWith(3, "/credentials/onepassword/import", {
      items: [{ vault_id: "vault-1", item_id: "item-new" }],
    });
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows a 403 and keeps the selection when every item retry is denied", async () => {
    const get = vi.fn(async (path: string) => {
      if (path === "/credentials/onepassword/items") {
        return {
          data: {
            configured: true,
            source: "organization",
            items: [
              {
                item_id: "item-1",
                title: "Example login",
                vault_id: "vault-1",
                vault_name: "Team vault",
                category: "LOGIN",
              },
            ],
          },
        };
      }
      if (path === "/credentials") return { data: [] };
      throw new Error(`Unexpected GET ${path}`);
    });
    const post = vi.fn().mockRejectedValue({
      isAxiosError: true,
      response: { status: 403, data: { detail: "forbidden" } },
    });
    mockedGetClient.mockResolvedValue({ get, post } as never);

    renderDialog();
    fireEvent.click(
      screen.getByRole("button", { name: "Import from 1Password" }),
    );
    fireEvent.click(
      await screen.findByRole("checkbox", { name: "Select Example login" }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Import selected items" }),
    );

    expect((await screen.findByRole("alert")).textContent).toContain(
      "You don’t have permission to import these 1Password items.",
    );
    expect(screen.getByText("1 selected")).toBeTruthy();
    expect(
      screen
        .getByRole("checkbox", { name: "Select Example login" })
        .getAttribute("aria-checked"),
    ).toBe("true");
    expect(post).toHaveBeenCalledTimes(2);
    expect(post).toHaveBeenNthCalledWith(1, "/credentials/onepassword/import", {
      items: [{ vault_id: "vault-1", item_id: "item-1" }],
    });
    expect(post).toHaveBeenNthCalledWith(2, "/credentials/onepassword/import", {
      items: [{ vault_id: "vault-1", item_id: "item-1" }],
    });
    expect(screen.queryByText("Import results")).toBeNull();
  });
});
