import { AxiosError } from "axios";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { useContext } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { OidcUser, OidcUserManager } from "@/auth/oidc";

const mocks = vi.hoisted(() => ({
  createOidcUserManager: vi.fn(),
  getClient: vi.fn(),
  removeAuthorizationHeader: vi.fn(),
  whenRuntimeCredentialReady: vi.fn().mockResolvedValue(undefined),
}));

vi.mock("@/auth/oidc", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/auth/oidc")>()),
  createOidcUserManager: mocks.createOidcUserManager,
}));
vi.mock("@/api/AxiosClient", () => ({
  getClient: mocks.getClient,
  removeAuthorizationHeader: mocks.removeAuthorizationHeader,
}));
vi.mock("@/util/env", () => ({
  whenRuntimeCredentialReady: mocks.whenRuntimeCredentialReady,
}));

import { OnPremOidcProvider } from "./OnPremOidcProvider";
import type { CredentialGetter } from "@/api/AxiosClient";
import { useHasCapability, usePageSlots } from "@/store/PageSlots";
import { OidcAuthContext } from "@/store/OnPremOidcContext";

const enabledConfig = {
  issuer_url: "https://issuer.example",
  client_id: "skyvern-ui",
  scopes: "openid profile",
};

const capabilitiesResponse = {
  organization_id: "org-id",
  user_id: "user-id",
  auth_kind: "bearer",
  org_role: null,
  role: null,
  capabilities: [
    { capability: "workflow.update", allowed: true, reason: "role" },
  ],
  rbac: { enabled: true, enforce: true, effective_role: null },
};

function jsonResponse(data: unknown) {
  return new Response(JSON.stringify(data), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function createManager(user: OidcUser | null) {
  const manager = {
    getUser: vi.fn().mockResolvedValue(user),
    signinRedirect: vi.fn().mockResolvedValue(undefined),
    signinRedirectCallback: vi.fn(),
    signinSilent: vi.fn().mockResolvedValue(user),
    removeUser: vi.fn().mockResolvedValue(undefined),
    signoutRedirect: vi.fn().mockResolvedValue(undefined),
    metadataService: {
      getEndSessionEndpoint: vi.fn().mockResolvedValue(undefined),
    },
  } as unknown as OidcUserManager;
  mocks.createOidcUserManager.mockReturnValue(manager);
  return manager;
}

function permissionProbe() {
  function Probe() {
    const { permissions } = usePageSlots();
    const allowed = useHasCapability("workflow.update");
    return (
      <output data-testid="permissions">
        {permissions?.status ?? "unset"}:{String(allowed)}
      </output>
    );
  }
  return <Probe />;
}

function SignOutProbe() {
  const { signOut } = useContext(OidcAuthContext);
  return <button onClick={() => void signOut()}>Test sign-out</button>;
}

function signedInUser(): OidcUser {
  return {
    access_token: "access-token-canary",
    refresh_token: "refresh-token-canary",
    expires_at: Math.floor(Date.now() / 1000) + 3600,
    id_token: "id-token-canary",
    state: undefined,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.whenRuntimeCredentialReady.mockResolvedValue(undefined);
  window.history.replaceState({}, "", "/");
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      jsonResponse({
        issuer_url: "",
        client_id: "",
        scopes: "openid profile",
      }),
    ),
  );
});

afterEach(() => {
  vi.restoreAllMocks();
  cleanup();
  vi.unstubAllGlobals();
  window.sessionStorage.clear();
});

describe("OnPremOidcProvider", () => {
  it("keeps the current UI and makes no IdP request when the issuer is unset", async () => {
    render(<OnPremOidcProvider>{permissionProbe()}</OnPremOidcProvider>);

    expect((await screen.findByTestId("permissions")).textContent).toContain(
      "unset:true",
    );
    expect(screen.queryByRole("button", { name: "Sign in" })).toBeNull();
    expect(mocks.createOidcUserManager).not.toHaveBeenCalled();
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch).toHaveBeenCalledWith("/oidc-config", expect.any(Object));
  });

  it("shows sign-in and starts the OIDC redirect when configured", async () => {
    window.history.replaceState(
      {},
      "",
      "/workflows/deep-link?tab=steps#details",
    );
    vi.mocked(fetch).mockResolvedValue(jsonResponse(enabledConfig));
    const manager = createManager(null);

    render(<OnPremOidcProvider>{permissionProbe()}</OnPremOidcProvider>);
    fireEvent.click(await screen.findByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(manager.signinRedirect).toHaveBeenCalled());
    expect(manager.signinRedirect).toHaveBeenCalledWith({
      state: { returnTo: "/workflows/deep-link?tab=steps#details" },
    });
  });

  it("maps the capabilities response into the permissions slot", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse(enabledConfig));
    createManager(signedInUser());
    const get = vi.fn().mockResolvedValue({ data: capabilitiesResponse });
    mocks.getClient.mockResolvedValue({ get });

    render(<OnPremOidcProvider>{permissionProbe()}</OnPremOidcProvider>);

    expect((await screen.findByTestId("permissions")).textContent).toContain(
      "ready:true",
    );
    expect(mocks.getClient).toHaveBeenCalledWith(expect.any(Function), "v1");
    expect(get).toHaveBeenCalledWith("/auth/capabilities");
  });

  it("waits for the UI session before returning a stored OIDC bearer", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse(enabledConfig));
    const manager = createManager(signedInUser());
    const get = vi.fn().mockResolvedValue({ data: capabilitiesResponse });
    let resolveReady!: () => void;
    mocks.whenRuntimeCredentialReady.mockImplementation(
      () => new Promise<void>((resolve) => (resolveReady = resolve)),
    );
    mocks.getClient.mockImplementation(
      async (credentialGetter: CredentialGetter) => {
        await credentialGetter();
        return { get };
      },
    );

    render(<OnPremOidcProvider>{permissionProbe()}</OnPremOidcProvider>);

    await waitFor(() => expect(mocks.getClient).toHaveBeenCalled());
    expect(manager.getUser).toHaveBeenCalledTimes(1);
    expect(get).not.toHaveBeenCalled();

    await act(async () => resolveReady());

    expect((await screen.findByTestId("permissions")).textContent).toContain(
      "ready:true",
    );
    expect(manager.getUser).toHaveBeenCalledTimes(2);
    expect(mocks.getClient).toHaveBeenCalledWith(expect.any(Function), "v1");
    expect(get).toHaveBeenCalledWith("/auth/capabilities");
  });

  it("silently refreshes an expired session when it has a refresh token", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse(enabledConfig));
    const user = {
      ...signedInUser(),
      expires_at: Math.floor(Date.now() / 1000) - 1,
    };
    const manager = createManager(user);
    manager.signinSilent = vi.fn().mockResolvedValue(signedInUser());
    mocks.getClient.mockResolvedValue({
      get: vi.fn().mockResolvedValue({ data: capabilitiesResponse }),
    });

    render(<OnPremOidcProvider>{permissionProbe()}</OnPremOidcProvider>);

    await waitFor(() => expect(manager.signinSilent).toHaveBeenCalled());
    expect(await screen.findByTestId("permissions")).toBeTruthy();
  });

  it("returns to sign-in at expiry when no refresh token exists", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse(enabledConfig));
    const user = {
      ...signedInUser(),
      refresh_token: undefined,
      expires_at: Math.floor(Date.now() / 1000) - 1,
    };
    const manager = createManager(user);

    render(<OnPremOidcProvider>{permissionProbe()}</OnPremOidcProvider>);

    expect(await screen.findByRole("button", { name: "Sign in" })).toBeTruthy();
    expect(manager.signinSilent).not.toHaveBeenCalled();
    expect(manager.removeUser).toHaveBeenCalled();
  });

  it("clears local state and redirects to the discovered end-session endpoint", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse(enabledConfig));
    const manager = createManager(signedInUser());
    manager.metadataService.getEndSessionEndpoint = vi
      .fn()
      .mockResolvedValue("https://issuer.example/logout");
    mocks.getClient.mockResolvedValue({
      get: vi.fn().mockResolvedValue({ data: capabilitiesResponse }),
    });

    render(
      <OnPremOidcProvider>
        <SignOutProbe />
      </OnPremOidcProvider>,
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "Test sign-out" }),
    );

    await waitFor(() => expect(manager.removeUser).toHaveBeenCalled());
    expect(mocks.removeAuthorizationHeader).toHaveBeenCalled();
    await waitFor(() =>
      expect(manager.signoutRedirect).toHaveBeenCalledWith({
        id_token_hint: "id-token-canary",
      }),
    );
  });

  it.each([401, 403])(
    "falls back to today's permission behavior on capabilities %s",
    async (status) => {
      vi.mocked(fetch).mockResolvedValue(jsonResponse(enabledConfig));
      createManager(signedInUser());
      const error = Object.assign(new AxiosError("Unauthorized"), {
        response: { status },
      });
      const get = vi.fn().mockRejectedValue(error);
      mocks.getClient.mockResolvedValue({ get });

      render(<OnPremOidcProvider>{permissionProbe()}</OnPremOidcProvider>);

      await waitFor(() =>
        expect(get).toHaveBeenCalledWith("/auth/capabilities"),
      );
      expect((await screen.findByTestId("permissions")).textContent).toContain(
        "unset:true",
      );
    },
  );

  it("retries transient capability failures", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse(enabledConfig));
    createManager(signedInUser());
    const error = Object.assign(new AxiosError("Service Unavailable"), {
      response: { status: 503, headers: { "retry-after": "2" } },
    });
    const get = vi
      .fn()
      .mockRejectedValueOnce(error)
      .mockResolvedValueOnce({ data: capabilitiesResponse });
    mocks.getClient.mockResolvedValue({ get });
    let retry!: () => void;
    const nativeSetTimeout = setTimeout.bind(globalThis);
    vi.spyOn(window, "setTimeout").mockImplementation((handler, timeout) => {
      if (timeout === 2_000) {
        retry = handler as () => void;
        return nativeSetTimeout(() => undefined, timeout);
      }
      return nativeSetTimeout(handler, timeout);
    });

    render(<OnPremOidcProvider>{permissionProbe()}</OnPremOidcProvider>);

    await waitFor(() => expect(get).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(retry).toBeDefined());
    expect((await screen.findByTestId("permissions")).textContent).toContain(
      "error:false",
    );

    await act(async () => retry());

    expect((await screen.findByTestId("permissions")).textContent).toContain(
      "ready:true",
    );
    expect(get).toHaveBeenCalledTimes(2);
  });
});
