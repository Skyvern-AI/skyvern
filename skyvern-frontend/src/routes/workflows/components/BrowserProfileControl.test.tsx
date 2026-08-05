// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import type { ReactNode } from "react";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";

import type { BrowserProfileApiResponse } from "@/api/types";
import { BrowserProfileControl } from "./BrowserProfileControl";

const mocks = vi.hoisted(() => ({ useInfiniteBrowserProfilesQuery: vi.fn() }));

vi.mock("../hooks/useInfiniteBrowserProfilesQuery", () => ({
  useInfiniteBrowserProfilesQuery: mocks.useInfiniteBrowserProfilesQuery,
}));
vi.mock("@/routes/browserProfiles/hooks/useBrowserProfileQuery", () => ({
  useBrowserProfileQuery: () => ({ data: undefined }),
}));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

const originalScrollIntoView = Element.prototype.scrollIntoView;
beforeAll(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  Element.prototype.scrollIntoView = () => {};
});
afterAll(() => {
  vi.unstubAllGlobals();
  Element.prototype.scrollIntoView = originalScrollIntoView;
});

function profile(id: string, name: string): BrowserProfileApiResponse {
  return {
    browser_profile_id: id,
    name,
    is_managed: false,
    linked_credential_name: null,
    deleted_at: null,
  } as unknown as BrowserProfileApiResponse;
}

function renderControl() {
  mocks.useInfiniteBrowserProfilesQuery.mockReturnValue({
    data: { pages: [[profile("bp_alpha", "Alpha Profile")]] },
    isFetching: false,
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      {children}
    </QueryClientProvider>
  );
  render(
    <BrowserProfileControl
      mode="dropdown"
      profileId={null}
      onProfileChange={vi.fn()}
      codeValue=""
      onCodeChange={vi.fn()}
      codeMode="none"
      restingCaption="Fresh browser every run"
    />,
    { wrapper },
  );
  fireEvent.click(screen.getByRole("combobox"));
}

const follows = (a: Element, b: Element) =>
  Boolean(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);

describe("BrowserProfileControl dropdown order", () => {
  it("renders ＋ New profile directly under Auto and above the profile list", () => {
    renderControl();

    // Scope to the menu — "Auto" also shows on the closed trigger label.
    const list = within(screen.getByRole("listbox"));
    const auto = list.getByText("Auto");
    const create = list.getByText("＋ New profile…");
    const firstProfile = list.getByText("Alpha Profile");

    expect(follows(auto, create)).toBe(true);
    expect(follows(create, firstProfile)).toBe(true);
  });

  it("still opens the inline create input from its new position", () => {
    renderControl();

    fireEvent.click(screen.getByText("＋ New profile…"));

    expect(screen.getByPlaceholderText("Profile name")).toBeTruthy();
  });

  it("drops ＋ New profile below the matches while searching (Auto hides, top match stays the Enter target)", async () => {
    renderControl();

    fireEvent.change(screen.getByPlaceholderText("Search profiles..."), {
      target: { value: "al" },
    });

    await waitFor(() => {
      const list = within(screen.getByRole("listbox"));
      expect(list.queryByText("Auto")).toBeNull();
      expect(
        follows(
          list.getByText("Alpha Profile"),
          list.getByText("＋ New profile…"),
        ),
      ).toBe(true);
    });
  });
});
