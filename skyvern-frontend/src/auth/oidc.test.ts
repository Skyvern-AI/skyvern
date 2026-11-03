import { readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, describe, expect, it, vi } from "vitest";

import { createOidcUserManager } from "./oidc";

const config = {
  issuerUrl: "https://issuer.example",
  clientId: "skyvern-ui",
  scopes: "openid profile",
};

const metadata = {
  issuer: config.issuerUrl,
  authorization_endpoint: `${config.issuerUrl}/authorize`,
  token_endpoint: `${config.issuerUrl}/token`,
  end_session_endpoint: `${config.issuerUrl}/logout`,
};

function jsonResponse(data: unknown) {
  return new Response(JSON.stringify(data), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function base64UrlEncode(bytes: Uint8Array) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary)
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

function sourceFiles(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) return sourceFiles(path);
    return /\.[cm]?[jt]sx?$/.test(entry.name) ? [path] : [];
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  window.sessionStorage.clear();
  window.localStorage.clear();
});

describe("OIDC browser client", () => {
  it("uses S256 PKCE and exchanges the validated authorization code", async () => {
    let redirectUrl = "";
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(metadata))
      .mockResolvedValueOnce(
        jsonResponse({
          access_token: "session-token-canary",
          refresh_token: "refresh-token-canary",
          id_token: "id-token-canary",
          token_type: "Bearer",
          expires_in: 3600,
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    const manager = createOidcUserManager(config, (url) => {
      redirectUrl = url;
    });

    await manager.signinRedirect({
      state: { returnTo: "/workflows/deep-link?tab=steps#details" },
    });

    const redirect = new URL(redirectUrl);
    const state = redirect.searchParams.get("state");
    const user = await manager.signinRedirectCallback(
      `${window.location.origin}/oidc/callback?code=authorization-code&state=${encodeURIComponent(state ?? "")}`,
    );
    const requestInit = fetchMock.mock.calls[1]?.[1] as RequestInit;
    const tokenRequest = new URLSearchParams(requestInit.body as string);
    const codeVerifier = tokenRequest.get("code_verifier") ?? "";
    const expectedChallenge = base64UrlEncode(
      new Uint8Array(
        await window.crypto.subtle.digest(
          "SHA-256",
          new TextEncoder().encode(codeVerifier),
        ),
      ),
    );

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      `${config.issuerUrl}/.well-known/openid-configuration`,
      expect.objectContaining({ cache: "no-store" }),
    );
    expect(redirect.searchParams.get("response_type")).toBe("code");
    expect(state).toMatch(/^[A-Za-z0-9_-]+$/);
    expect(redirect.searchParams.get("code_challenge_method")).toBe("S256");
    expect(redirect.searchParams.get("code_challenge")).toBe(expectedChallenge);
    expect(tokenRequest.get("grant_type")).toBe("authorization_code");
    expect(tokenRequest.get("code")).toBe("authorization-code");
    expect(tokenRequest.get("client_id")).toBe(config.clientId);
    expect(tokenRequest.get("redirect_uri")).toBe(
      `${window.location.origin}/oidc/callback`,
    );
    expect(codeVerifier).toMatch(/^[A-Za-z0-9_-]+$/);
    expect(user.access_token).toBe("session-token-canary");
    expect(user.state).toEqual({
      returnTo: "/workflows/deep-link?tab=steps#details",
    });
    expect(window.sessionStorage.length).toBeGreaterThan(0);
    expect(
      window.sessionStorage.getItem(window.sessionStorage.key(0)!),
    ).toContain("session-token-canary");
    expect(window.localStorage.length).toBe(0);
  });

  it("rejects a callback with no matching state", async () => {
    let redirectUrl = "";
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(metadata))
      .mockResolvedValueOnce(
        jsonResponse({
          access_token: "unexpected-token",
          token_type: "Bearer",
          expires_in: 3600,
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    const manager = createOidcUserManager(config, (url) => {
      redirectUrl = url;
    });
    await manager.signinRedirect();
    const state = new URL(redirectUrl).searchParams.get("state");

    await expect(
      manager.signinRedirectCallback(
        `${window.location.origin}/oidc/callback?code=authorization-code&state=wrong-${state}`,
      ),
    ).rejects.toThrow("OIDC callback state is invalid");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does not import cloud-only packages in the self-hosted frontend source", () => {
    const src = join(dirname(fileURLToPath(import.meta.url)), "..");
    const forbiddenImport =
      /\b(?:from|import)\s*(?:\(\s*)?["'](?:@(?:clerk|cloud)\/|cloud(?:\/|["']))/;
    expect(
      forbiddenImport.test(['import { value } from "', '@cloud/api"'].join("")),
    ).toBe(true);
    const importedFiles = sourceFiles(src).filter((path) =>
      forbiddenImport.test(readFileSync(path, "utf8")),
    );

    expect(importedFiles).toEqual([]);
  });

  it("normalizes trailing slashes when matching the discovery issuer", async () => {
    let redirectUrl = "";
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(metadata)));
    const manager = createOidcUserManager(
      { ...config, issuerUrl: `${config.issuerUrl}/` },
      (url) => {
        redirectUrl = url;
      },
    );

    await manager.signinRedirect();

    expect(redirectUrl).toBeTruthy();
    expect(fetch).toHaveBeenCalledWith(
      `${config.issuerUrl}/.well-known/openid-configuration`,
      expect.objectContaining({ cache: "no-store" }),
    );
  });

  it("rejects an HTTP issuer before making any IdP request", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    expect(() =>
      createOidcUserManager({ ...config, issuerUrl: "http://issuer.example" }),
    ).toThrow("OIDC issuer URL is invalid");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects a token response without an expiry", async () => {
    let redirectUrl = "";
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(jsonResponse(metadata))
        .mockResolvedValueOnce(jsonResponse({ access_token: "access-token" })),
    );
    const manager = createOidcUserManager(config, (url) => {
      redirectUrl = url;
    });
    await manager.signinRedirect();
    const state = new URL(redirectUrl).searchParams.get("state");

    await expect(
      manager.signinRedirectCallback(
        `${window.location.origin}/oidc/callback?code=authorization-code&state=${encodeURIComponent(state ?? "")}`,
      ),
    ).rejects.toThrow("OIDC token response has no valid expiry");
  });

  it("rejects discovery endpoints on a different origin", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({
          ...metadata,
          token_endpoint: "https://tokens.example/token",
        }),
      ),
    );
    const manager = createOidcUserManager(config, () => undefined);

    await expect(manager.signinRedirect()).rejects.toThrow(
      "OIDC discovery document has an invalid token_endpoint",
    );
  });
});
