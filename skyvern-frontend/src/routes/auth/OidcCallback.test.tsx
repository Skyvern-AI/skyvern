import { StrictMode } from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { OidcUser, OidcUserManager } from "@/auth/oidc";

const mocks = vi.hoisted(() => ({
  removeAuthorizationHeader: vi.fn(),
}));
let callbackCounter = 0;

vi.mock("@/api/AxiosClient", () => ({
  removeAuthorizationHeader: mocks.removeAuthorizationHeader,
}));

import { OidcCallback } from "./OidcCallback";
import { OidcAuthContext } from "@/store/OnPremOidcContext";

function LocationProbe() {
  const location = useLocation();
  return (
    <output data-testid="location">
      {location.pathname}
      {location.search}
      {location.hash}
    </output>
  );
}

function renderCallback(returnTo: unknown) {
  const callbackUrl = `/oidc/callback?code=authorization-code&state=oidc-state-${++callbackCounter}`;
  const user = {
    access_token: "access-token",
    expires_at: Math.floor(Date.now() / 1000) + 3600,
    state: { returnTo },
  } as OidcUser;
  const manager = {
    signinRedirectCallback: vi.fn().mockResolvedValue(user),
    removeUser: vi.fn().mockResolvedValue(undefined),
  } as unknown as OidcUserManager;
  const completeSignIn = vi.fn();

  window.history.replaceState({}, "", callbackUrl);
  render(
    <StrictMode>
      <OidcAuthContext.Provider
        value={{
          enabled: true,
          manager,
          user: null,
          signIn: async () => undefined,
          signOut: async () => undefined,
          completeSignIn,
        }}
      >
        <MemoryRouter initialEntries={[callbackUrl]}>
          <Routes>
            <Route path="/oidc/callback" element={<OidcCallback />} />
            <Route path="*" element={<LocationProbe />} />
          </Routes>
        </MemoryRouter>
      </OidcAuthContext.Provider>
    </StrictMode>,
  );

  return { manager, completeSignIn, user };
}

afterEach(() => {
  vi.clearAllMocks();
  window.history.replaceState({}, "", "/");
});

describe("OIDC callback", () => {
  it("processes the response once in StrictMode and restores the validated route", async () => {
    const { manager, completeSignIn, user } = renderCallback(
      "/workflows/deep-link?tab=steps#details",
    );

    expect((await screen.findByTestId("location")).textContent).toBe(
      "/workflows/deep-link?tab=steps#details",
    );
    expect(manager.signinRedirectCallback).toHaveBeenCalledTimes(1);
    expect(completeSignIn).toHaveBeenCalledWith(user);
  });

  it("falls back to the home route for an external return URL", async () => {
    const { manager } = renderCallback("//attacker.example/workflows");

    expect((await screen.findByTestId("location")).textContent).toBe("/");
    expect(manager.signinRedirectCallback).toHaveBeenCalledTimes(1);
  });
});
