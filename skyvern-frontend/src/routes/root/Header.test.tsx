import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { OidcAuthContext } from "@/store/OnPremOidcContext";
import { Header } from "./Header";

vi.mock("./NavigationHamburgerMenu", () => ({
  NavigationHamburgerMenu: () => <div data-testid="hamburger">Menu</div>,
}));

vi.mock("./useSidebarHidden", () => ({
  useSidebarHidden: () => false,
}));

describe("Header", () => {
  it("keeps sign out after the hamburger and right aligned", () => {
    render(
      <OidcAuthContext.Provider
        value={{
          enabled: true,
          manager: null,
          user: { access_token: "test-token", expires_at: 0, state: null },
          signIn: async () => undefined,
          signOut: async () => undefined,
          completeSignIn: () => undefined,
        }}
      >
        <Header />
      </OidcAuthContext.Provider>,
    );

    const hamburger = screen.getByTestId("hamburger");
    const signOut = screen.getByRole("button", { name: "Sign out" });

    expect(
      hamburger.compareDocumentPosition(signOut) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(signOut.classList.contains("ml-auto")).toBe(true);
  });
});
