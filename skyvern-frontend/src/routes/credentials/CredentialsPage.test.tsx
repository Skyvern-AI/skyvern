// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useTotpCodesQuery } from "@/hooks/useTotpCodesQuery";
import { CredentialsPage } from "./CredentialsPage";

vi.mock("@/components/PushTotpCodeForm", () => ({
  PushTotpCodeForm: () => <div />,
}));

vi.mock("@/hooks/useTotpCodesQuery", () => ({
  useTotpCodesQuery: vi.fn(),
}));

vi.mock("./useCredentialModalState", () => ({
  CredentialModalTypes: {
    PASSWORD: "password",
    CREDIT_CARD: "credit_card",
    SECRET: "secret",
  },
  useCredentialModalState: () => ({
    setIsOpen: vi.fn(),
    setType: vi.fn(),
  }),
}));

vi.mock("./useBackgroundCredentialTest", () => ({
  useBackgroundCredentialTest: () => ({
    startBackgroundTest: vi.fn(),
  }),
}));

vi.mock("./hooks/useCredentialFoldersQuery", () => ({
  useCredentialFoldersQuery: () => ({
    data: [],
    isLoading: false,
  }),
}));

vi.mock("./CredentialsList", () => ({
  CredentialsList: () => <div />,
}));

vi.mock("./BitwardenCredentialsList", () => ({
  BitwardenCredentialsList: () => <div />,
}));

vi.mock("./OnePasswordCredentialsList", () => ({
  OnePasswordCredentialsList: () => <div />,
}));

vi.mock("./CredentialsModal", () => ({
  CredentialsModal: () => <div />,
}));

vi.mock("./CreateCredentialFolderDialog", () => ({
  CreateCredentialFolderDialog: () => <div />,
}));

vi.mock("./ViewAllCredentialFoldersDialog", () => ({
  ViewAllCredentialFoldersDialog: () => <div />,
}));

const mockedUseTotpCodesQuery = vi.mocked(useTotpCodesQuery);

function LocationSearch() {
  return <output data-testid="location-search">{useLocation().search}</output>;
}

function renderPage(url: string) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[url]}>
        <CredentialsPage />
        <LocationSearch />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.restoreAllMocks();
});

describe("CredentialsPage tabs", () => {
  it("describes support for 2FA codes and magic links", () => {
    renderPage("/credentials");

    expect(
      screen.getByText(
        "Securely store your passwords, credit cards, secrets, and manage incoming 2FA codes and magic links for your agents.",
      ),
    ).toBeTruthy();
  });

  it("scrolls the tab strip horizontally on mount and tab changes", async () => {
    mockedUseTotpCodesQuery.mockReturnValue({
      data: [],
      isLoading: false,
      isFetching: false,
      isFeatureUnavailable: false,
    } as unknown as ReturnType<typeof useTotpCodesQuery>);
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(
      function (this: Element) {
        const isScrollContainer =
          this instanceof HTMLElement &&
          this.classList.contains("overflow-x-auto");
        const tabName = this.textContent?.trim();
        const left = isScrollContainer
          ? 0
          : tabName === "Magic Links"
            ? 400
            : tabName === "Passwords"
              ? -200
              : 0;
        const width = isScrollContainer ? 300 : 100;

        return {
          bottom: 0,
          height: 0,
          left,
          right: left + width,
          top: 0,
          width,
          x: left,
          y: 0,
          toJSON: () => ({}),
        };
      },
    );
    const scrollLeftSetter = vi.spyOn(Element.prototype, "scrollLeft", "set");

    renderPage("/credentials?tab=magicLinks");

    expect(scrollLeftSetter).toHaveBeenCalledWith(200);

    scrollLeftSetter.mockClear();
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Passwords" }));

    await waitFor(() => {
      expect(scrollLeftSetter).toHaveBeenCalledWith(0);
    });
  });

  it("re-scrolls the active tab when resizing makes the strip overflow", () => {
    mockedUseTotpCodesQuery.mockReturnValue({
      data: [],
      isLoading: false,
      isFetching: false,
      isFeatureUnavailable: false,
    } as unknown as ReturnType<typeof useTotpCodesQuery>);
    let containerWidth = 500;
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(
      function (this: Element) {
        const isScrollContainer =
          this instanceof HTMLElement &&
          this.classList.contains("overflow-x-auto");
        const left =
          !isScrollContainer && this.textContent?.trim() === "Magic Links"
            ? 400
            : 0;
        const width = isScrollContainer ? containerWidth : 100;

        return {
          bottom: 0,
          height: 0,
          left,
          right: left + width,
          top: 0,
          width,
          x: left,
          y: 0,
          toJSON: () => ({}),
        };
      },
    );
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(
      function (this: HTMLElement) {
        return this.classList.contains("overflow-x-auto") ? containerWidth : 0;
      },
    );
    vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockImplementation(
      function (this: HTMLElement) {
        return this.classList.contains("overflow-x-auto") ? 500 : 0;
      },
    );
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
      callback(0);
      return 1;
    });
    const scrollLeftSetter = vi.spyOn(Element.prototype, "scrollLeft", "set");

    renderPage("/credentials?tab=magicLinks");
    scrollLeftSetter.mockClear();
    containerWidth = 300;

    fireEvent(window, new Event("resize"));

    expect(scrollLeftSetter).toHaveBeenCalledWith(200);
  });

  it("selects the Magic Links tab from the URL and renders its empty state without an icon", () => {
    mockedUseTotpCodesQuery.mockReturnValue({
      data: [],
      isLoading: false,
      isFetching: false,
      isFeatureUnavailable: false,
    } as unknown as ReturnType<typeof useTotpCodesQuery>);

    renderPage("/credentials?tab=magicLinks");

    expect(
      screen
        .getByRole("tab", { name: "Magic Links" })
        .getAttribute("aria-selected"),
    ).toBe("true");
    expect(
      screen.getByRole("tab", { name: "Magic Links" }).querySelector("svg"),
    ).toBeNull();
    expect(
      screen.getByText(
        "No magic links yet. Paste a magic link message above or connect an email account.",
      ),
    ).toBeTruthy();
    expect(screen.queryByPlaceholderText("Search credentials…")).toBeNull();
    expect(screen.queryByText("self-hosted Bitwarden")).toBeNull();
  });

  it("falls back to passwords and normalizes an invalid tab parameter", async () => {
    mockedUseTotpCodesQuery.mockReturnValue({
      data: [],
      isLoading: false,
      isFetching: false,
      isFeatureUnavailable: false,
    } as unknown as ReturnType<typeof useTotpCodesQuery>);

    renderPage("/credentials?tab=not-a-tab");

    expect(
      screen
        .getByRole("tab", { name: "Passwords" })
        .getAttribute("aria-selected"),
    ).toBe("true");
    await waitFor(() => {
      expect(screen.getByTestId("location-search").textContent).toBe(
        "?tab=passwords",
      );
    });
  });
});
