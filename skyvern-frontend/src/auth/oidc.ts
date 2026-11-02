export type OidcRuntimeConfig = Readonly<{
  issuerUrl: string;
  clientId: string;
  scopes: string;
}>;

export type OidcUser = Readonly<{
  access_token: string;
  refresh_token?: string;
  id_token?: string;
  expires_at: number;
  state: unknown;
}>;

export interface OidcUserManager {
  getUser(): Promise<OidcUser | null>;
  signinRedirect(args?: { state?: unknown }): Promise<void>;
  signinRedirectCallback(url?: string): Promise<OidcUser>;
  signinSilent(): Promise<OidcUser | null>;
  removeUser(): Promise<void>;
  signoutRedirect(args?: { id_token_hint?: string }): Promise<void>;
  metadataService: {
    getEndSessionEndpoint(): Promise<string | undefined>;
  };
}

type OidcMetadata = Readonly<{
  authorizationEndpoint: string;
  tokenEndpoint: string;
  endSessionEndpoint?: string;
}>;

type PendingSignin = Readonly<{
  state: string;
  codeVerifier: string;
  userState: unknown;
}>;

type TokenResponse = Readonly<{
  access_token: string;
  refresh_token?: string;
  id_token?: string;
  expires_at: number;
}>;

const USER_STORAGE_KEY = "skyvern.oidc.user";
const SIGNIN_STORAGE_KEY = "skyvern.oidc.pending-signin";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export async function loadOidcRuntimeConfig(): Promise<OidcRuntimeConfig> {
  const response = await fetch("/oidc-config", {
    cache: "no-store",
    headers: { accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error("OIDC runtime configuration is unavailable");
  }

  const payload: unknown = await response.json();
  if (
    !isRecord(payload) ||
    typeof payload.issuer_url !== "string" ||
    typeof payload.client_id !== "string" ||
    typeof payload.scopes !== "string"
  ) {
    throw new Error("OIDC runtime configuration is invalid");
  }

  return {
    issuerUrl: payload.issuer_url.trim(),
    clientId: payload.client_id.trim(),
    scopes: payload.scopes.trim() || "openid profile",
  };
}

function encodeBase64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary)
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

function randomBase64Url(): string {
  const bytes = new Uint8Array(32);
  window.crypto.getRandomValues(bytes);
  return encodeBase64Url(bytes);
}

function endpoint(value: unknown, name: string, issuerOrigin: string): string {
  if (typeof value !== "string") {
    throw new Error(`OIDC discovery document has no ${name}`);
  }
  const url = new URL(value);
  if (
    (url.protocol !== "https:" && url.protocol !== "http:") ||
    url.origin !== issuerOrigin ||
    url.username ||
    url.password
  ) {
    throw new Error(`OIDC discovery document has an invalid ${name}`);
  }
  return url.href;
}

function parseDiscovery(payload: unknown, issuerUrl: string): OidcMetadata {
  if (
    !isRecord(payload) ||
    typeof payload.issuer !== "string" ||
    payload.issuer.replace(/\/+$/, "") !== issuerUrl.replace(/\/+$/, "")
  ) {
    throw new Error(
      "OIDC discovery issuer does not match the configured issuer",
    );
  }
  const issuerOrigin = new URL(issuerUrl).origin;
  const endSessionEndpoint = payload.end_session_endpoint
    ? endpoint(
        payload.end_session_endpoint,
        "end_session_endpoint",
        issuerOrigin,
      )
    : undefined;
  return {
    authorizationEndpoint: endpoint(
      payload.authorization_endpoint,
      "authorization_endpoint",
      issuerOrigin,
    ),
    tokenEndpoint: endpoint(
      payload.token_endpoint,
      "token_endpoint",
      issuerOrigin,
    ),
    ...(endSessionEndpoint ? { endSessionEndpoint } : {}),
  };
}

function parseTokenResponse(payload: unknown): TokenResponse {
  if (
    !isRecord(payload) ||
    typeof payload.access_token !== "string" ||
    !payload.access_token
  ) {
    throw new Error("OIDC token response is invalid");
  }

  const expiresIn = payload.expires_in;
  // shortcut: token responses without expires_in are rejected; add provider-specific lifetime handling if needed.
  if (
    typeof expiresIn !== "number" ||
    !Number.isFinite(expiresIn) ||
    expiresIn <= 0
  ) {
    throw new Error("OIDC token response has no valid expiry");
  }
  return {
    access_token: payload.access_token,
    ...(typeof payload.refresh_token === "string"
      ? { refresh_token: payload.refresh_token }
      : {}),
    ...(typeof payload.id_token === "string"
      ? { id_token: payload.id_token }
      : {}),
    expires_at: Math.floor(Date.now() / 1000 + expiresIn),
  };
}

function readPendingSignin(): PendingSignin | null {
  const value = window.sessionStorage.getItem(SIGNIN_STORAGE_KEY);
  if (!value) return null;

  try {
    const pending: unknown = JSON.parse(value);
    if (
      isRecord(pending) &&
      typeof pending.state === "string" &&
      pending.state.length > 0 &&
      typeof pending.codeVerifier === "string" &&
      pending.codeVerifier.length > 0
    ) {
      return {
        state: pending.state,
        codeVerifier: pending.codeVerifier,
        userState: pending.userState,
      };
    }
  } catch {
    return null;
  }
  return null;
}

function readStoredUser(): OidcUser | null {
  const value = window.sessionStorage.getItem(USER_STORAGE_KEY);
  if (!value) return null;

  let user: unknown;
  try {
    user = JSON.parse(value);
  } catch {
    window.sessionStorage.removeItem(USER_STORAGE_KEY);
    return null;
  }
  if (
    isRecord(user) &&
    typeof user.access_token === "string" &&
    user.access_token.length > 0 &&
    typeof user.expires_at === "number" &&
    Number.isFinite(user.expires_at)
  ) {
    return {
      access_token: user.access_token,
      ...(typeof user.refresh_token === "string"
        ? { refresh_token: user.refresh_token }
        : {}),
      ...(typeof user.id_token === "string" ? { id_token: user.id_token } : {}),
      expires_at: user.expires_at,
      state: user.state,
    };
  }
  window.sessionStorage.removeItem(USER_STORAGE_KEY);
  return null;
}

async function requestTokens(
  tokenEndpoint: string,
  form: Record<string, string>,
): Promise<TokenResponse> {
  const response = await fetch(tokenEndpoint, {
    method: "POST",
    cache: "no-store",
    headers: {
      accept: "application/json",
      "content-type": "application/x-www-form-urlencoded",
    },
    body: new URLSearchParams(form).toString(),
  });
  if (!response.ok) throw new Error("OIDC token request failed");
  return parseTokenResponse(await response.json());
}

export function isOidcUserExpired(user: OidcUser): boolean {
  return user.expires_at * 1000 <= Date.now();
}

export function createOidcUserManager(
  config: OidcRuntimeConfig,
  redirect: (url: string) => void = (url) => window.location.assign(url),
): OidcUserManager {
  if (!config.issuerUrl || !config.clientId) {
    throw new Error("OIDC issuer and client ID are required");
  }
  const issuer = new URL(config.issuerUrl);
  if (
    issuer.protocol !== "https:" ||
    issuer.username ||
    issuer.password ||
    issuer.search ||
    issuer.hash
  ) {
    throw new Error("OIDC issuer URL is invalid");
  }
  if (!config.scopes.split(/\s+/).includes("openid")) {
    throw new Error("OIDC scopes must include openid");
  }

  const redirectUri = `${window.location.origin}/oidc/callback`;
  let discoveryPromise: Promise<OidcMetadata> | undefined;
  const getDiscovery = () => {
    if (!discoveryPromise) {
      const discoveryUrl = `${config.issuerUrl.replace(/\/+$/, "")}/.well-known/openid-configuration`;
      discoveryPromise = fetch(discoveryUrl, {
        cache: "no-store",
        headers: { accept: "application/json" },
      })
        .then(async (response) => {
          if (!response.ok) throw new Error("OIDC discovery request failed");
          return parseDiscovery(await response.json(), config.issuerUrl);
        })
        .catch((error: unknown) => {
          discoveryPromise = undefined;
          throw error;
        });
    }
    return discoveryPromise;
  };

  const storeUser = (user: OidcUser) => {
    window.sessionStorage.setItem(USER_STORAGE_KEY, JSON.stringify(user));
  };

  return {
    async getUser() {
      return readStoredUser();
    },
    async signinRedirect(args) {
      if (!window.crypto?.subtle) {
        throw new Error("Web Crypto is required for OIDC sign-in");
      }
      const state = randomBase64Url();
      const codeVerifier = randomBase64Url();
      const codeChallenge = encodeBase64Url(
        new Uint8Array(
          await window.crypto.subtle.digest(
            "SHA-256",
            new TextEncoder().encode(codeVerifier),
          ),
        ),
      );
      const metadata = await getDiscovery();
      window.sessionStorage.setItem(
        SIGNIN_STORAGE_KEY,
        JSON.stringify({ state, codeVerifier, userState: args?.state }),
      );
      const authorizationUrl = new URL(metadata.authorizationEndpoint);
      authorizationUrl.searchParams.set("response_type", "code");
      authorizationUrl.searchParams.set("client_id", config.clientId);
      authorizationUrl.searchParams.set("redirect_uri", redirectUri);
      authorizationUrl.searchParams.set("scope", config.scopes);
      authorizationUrl.searchParams.set("state", state);
      authorizationUrl.searchParams.set("code_challenge", codeChallenge);
      authorizationUrl.searchParams.set("code_challenge_method", "S256");
      redirect(authorizationUrl.toString());
    },
    async signinRedirectCallback(url = window.location.href) {
      const callbackUrl = new URL(url, window.location.origin);
      if (callbackUrl.origin !== window.location.origin) {
        throw new Error("OIDC callback URL is invalid");
      }

      const pending = readPendingSignin();
      window.sessionStorage.removeItem(SIGNIN_STORAGE_KEY);
      const state = callbackUrl.searchParams.get("state");
      const code = callbackUrl.searchParams.get("code");
      if (callbackUrl.searchParams.has("error")) {
        throw new Error("OIDC authorization failed");
      }
      if (!pending || !state || state !== pending.state || !code) {
        throw new Error("OIDC callback state is invalid");
      }

      const metadata = await getDiscovery();
      const tokens = await requestTokens(metadata.tokenEndpoint, {
        grant_type: "authorization_code",
        code,
        client_id: config.clientId,
        redirect_uri: redirectUri,
        code_verifier: pending.codeVerifier,
      });
      const user = { ...tokens, state: pending.userState };
      storeUser(user);
      return user;
    },
    async signinSilent() {
      const user = readStoredUser();
      if (!user?.refresh_token) return null;

      const metadata = await getDiscovery();
      const tokens = await requestTokens(metadata.tokenEndpoint, {
        grant_type: "refresh_token",
        refresh_token: user.refresh_token,
        client_id: config.clientId,
      });
      const refreshedUser = {
        ...user,
        ...tokens,
        refresh_token: tokens.refresh_token ?? user.refresh_token,
        id_token: tokens.id_token ?? user.id_token,
        state: user.state,
      };
      storeUser(refreshedUser);
      return refreshedUser;
    },
    async removeUser() {
      window.sessionStorage.removeItem(USER_STORAGE_KEY);
      window.sessionStorage.removeItem(SIGNIN_STORAGE_KEY);
    },
    async signoutRedirect(args) {
      const endpoint = (await getDiscovery()).endSessionEndpoint;
      if (!endpoint) return;
      const url = new URL(endpoint);
      if (args?.id_token_hint)
        url.searchParams.set("id_token_hint", args.id_token_hint);
      url.searchParams.set("post_logout_redirect_uri", window.location.origin);
      redirect(url.toString());
    },
    metadataService: {
      async getEndSessionEndpoint() {
        return (await getDiscovery()).endSessionEndpoint;
      },
    },
  };
}
