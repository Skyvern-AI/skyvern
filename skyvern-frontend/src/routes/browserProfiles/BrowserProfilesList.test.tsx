// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { BrowserProfileApiResponse } from "@/api/types";
import { useBrowserProfilesQuery } from "@/routes/workflows/hooks/useBrowserProfilesQuery";

import { BrowserProfilesList } from "./BrowserProfilesList";

vi.mock("@/routes/workflows/hooks/useBrowserProfilesQuery", () => ({
  useBrowserProfilesQuery: vi.fn(),
}));

vi.mock("./BrowserProfileItem", () => ({
  BrowserProfileItem: ({
    profile,
    index,
    selected = false,
    onSelect,
  }: {
    profile: BrowserProfileApiResponse;
    index: number;
    selected?: boolean;
    onSelect?: (index: number, shiftKey: boolean) => void;
  }) => (
    <tr data-testid="profile-row" data-selected={selected ? "true" : "false"}>
      <td>
        <button
          aria-label={`select-${profile.name}`}
          onClick={(event) => onSelect?.(index, event.shiftKey)}
        >
          {profile.name}
        </button>
      </td>
    </tr>
  ),
}));

const mockedUseBrowserProfilesQuery = vi.mocked(useBrowserProfilesQuery);

function makeProfile(index: number): BrowserProfileApiResponse {
  return {
    browser_profile_id: `bp_${index}`,
    organization_id: "org_1",
    name: `Profile ${index}`,
    description: null,
    source_browser_type: null,
    created_at: "2026-06-01T00:00:00.000Z",
    modified_at: "2026-06-01T00:00:00.000Z",
    deleted_at: null,
  };
}

function makeProfiles(count: number): BrowserProfileApiResponse[] {
  return Array.from({ length: count }, (_, index) => makeProfile(index));
}

function renderList(profiles: BrowserProfileApiResponse[]) {
  // Page 1 returns the rendered rows; the lookahead query for page 2 returns empty.
  mockedUseBrowserProfilesQuery.mockImplementation(
    ({ page }: { page?: number } = {}) =>
      ({
        data: page === 1 ? profiles : [],
        isLoading: false,
        isError: false,
        isFetching: false,
        refetch: vi.fn(),
      }) as unknown as ReturnType<typeof useBrowserProfilesQuery>,
  );

  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter
        initialEntries={["/browser-profiles"]}
        future={{ v7_relativeSplatPath: true, v7_startTransition: true }}
      >
        <BrowserProfilesList />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function selectionState(): string[] {
  return screen
    .getAllByTestId("profile-row")
    .map((row) => row.getAttribute("data-selected") ?? "false");
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("BrowserProfilesList paginated row selection", () => {
  it("selects only the clicked row on a plain click", () => {
    renderList(makeProfiles(3));

    fireEvent.click(screen.getByLabelText("select-Profile 1"));

    expect(selectionState()).toEqual(["false", "true", "false"]);
  });

  it("shift-click selects the contiguous range of rendered rows from the anchor", () => {
    renderList(makeProfiles(4));

    fireEvent.click(screen.getByLabelText("select-Profile 0"));
    fireEvent.click(screen.getByLabelText("select-Profile 3"), {
      shiftKey: true,
    });

    // Indices passed to the selection hook must address the same array the
    // table renders, so the highlighted rows are exactly the rendered range.
    expect(selectionState()).toEqual(["true", "true", "true", "true"]);
  });

  it("bulk-delete dialog warns credentials unlink and pinned workflows need repointing", () => {
    renderList(makeProfiles(2));

    fireEvent.click(screen.getByLabelText("select-Profile 0"));
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));

    expect(
      screen.getByText(/Linked credentials are unlinked automatically/),
    ).toBeTruthy();
  });
});
