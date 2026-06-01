// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { CredentialApiResponse } from "@/api/types";
import { useCredentialsQuery } from "@/routes/workflows/hooks/useCredentialsQuery";
import { CredentialsList } from "./CredentialsList";

vi.mock("@/routes/workflows/hooks/useCredentialsQuery", () => ({
  useCredentialsQuery: vi.fn(),
}));

vi.mock("./CredentialItem", () => ({
  CredentialItem: ({ credential }: { credential: CredentialApiResponse }) => (
    <div data-testid="credential-item">{credential.name}</div>
  ),
}));

const mockedUseCredentialsQuery = vi.mocked(useCredentialsQuery);

function mockResult(data: CredentialApiResponse[]) {
  mockedUseCredentialsQuery.mockReturnValue({
    data,
    isLoading: false,
  } as unknown as ReturnType<typeof useCredentialsQuery>);
}

function makeCredential(
  overrides: Partial<CredentialApiResponse> = {},
): CredentialApiResponse {
  return {
    credential_id: "cred_1",
    name: "Example",
    credential_type: "password",
    credential: { username: "user@example.com", totp_type: "none" },
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("CredentialsList — server-side search + type filtering (SKY-5679)", () => {
  it("requests the active tab's credential_type and the trimmed search term", () => {
    mockResult([]);

    render(<CredentialsList filter="password" search="  ohio  " />);

    expect(mockedUseCredentialsQuery).toHaveBeenCalledWith(
      expect.objectContaining({ credential_type: "password", search: "ohio" }),
    );
  });

  it("omits the search param when the term is only whitespace", () => {
    mockResult([]);

    render(<CredentialsList filter="secret" search="   " />);

    expect(mockedUseCredentialsQuery).toHaveBeenCalledWith(
      expect.objectContaining({ credential_type: "secret", search: undefined }),
    );
  });

  it("renders every credential the server returns (no client-side type filter)", () => {
    mockResult([
      makeCredential({ credential_id: "cred_1", name: "Alpha" }),
      makeCredential({ credential_id: "cred_2", name: "Beta" }),
    ]);

    render(<CredentialsList filter="password" />);

    expect(screen.getAllByTestId("credential-item")).toHaveLength(2);
    expect(screen.getByText("Alpha")).toBeTruthy();
    expect(screen.getByText("Beta")).toBeTruthy();
  });

  it("shows a search-aware empty state when a search yields no results", () => {
    mockResult([]);

    render(<CredentialsList filter="password" search="zzz" />);

    expect(screen.getByText(/No credentials match/)).toBeTruthy();
    expect(screen.getByText(/zzz/)).toBeTruthy();
  });

  it("shows the type-specific empty state when there is no search", () => {
    mockResult([]);

    render(<CredentialsList filter="password" />);

    expect(
      screen.getByText("No password credentials stored yet."),
    ).toBeTruthy();
  });
});
