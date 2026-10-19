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
import { CredentialFolderSelector } from "./CredentialFolderSelector";

vi.mock("@/api/AxiosClient", () => ({ getClient: vi.fn() }));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

const mockedGetClient = vi.mocked(getClient);

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("CredentialFolderSelector selection mode", () => {
  it("returns the chosen folder without assigning it to a credential", async () => {
    const folder = {
      folder_id: "folder-1",
      organization_id: "org-1",
      title: "Team folder",
      description: null,
      credential_count: 0,
      created_at: "2026-10-18T00:00:00Z",
      modified_at: "2026-10-18T00:00:00Z",
    };
    const get = vi.fn().mockResolvedValue({ data: [folder] });
    mockedGetClient.mockResolvedValue({ get } as never);
    const onFolderSelect = vi.fn();
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    render(
      <QueryClientProvider client={queryClient}>
        <CredentialFolderSelector
          currentFolderId={null}
          onFolderSelect={onFolderSelect}
          trigger={<button>Select target folder</button>}
        />
      </QueryClientProvider>,
    );

    fireEvent.click(
      screen.getByRole("button", { name: "Select target folder" }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Team folder" }));

    await waitFor(() => expect(onFolderSelect).toHaveBeenCalledWith(folder));
    expect(get).toHaveBeenCalledWith(
      "/credential_folders",
      expect.objectContaining({ params: expect.any(URLSearchParams) }),
    );
  });
});
