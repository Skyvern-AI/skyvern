import { createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { createServer } from "http";
import { fileURLToPath, pathToFileURL } from "node:url";
import { parseEnv } from "node:util";
import handler from "serve-handler";
import open from "open";

const DEFAULT_API_BASE_URL = "http://localhost:8000/api/v1";
const COMPOSE_API_BASE_URL = "http://skyvern:8000/api/v1";
const UI_SESSION_REQUEST_TIMEOUT_MS = 10_000;
const UI_SESSION_CACHE_EXPIRY_BUFFER_MS = 30_000;
const UI_SESSION_NONCE_COOKIE = "skyvern_ui_session_nonce";
const LOOPBACK_HOSTNAMES = new Set(["localhost", "127.0.0.1", "[::1]"]);

const UI_SESSION_NOT_CONFIGURED = "not_configured";
const UI_SESSION_UPSTREAM_UNREACHABLE = "upstream_unreachable";
const UI_SESSION_UPSTREAM_REJECTED = "upstream_rejected";
const UI_SESSION_UPSTREAM_INVALID = "upstream_invalid_response";

// Reaches the browser, so it names the variable to change but never the upstream address.
const UI_SESSION_FAILURE_DETAIL = {
  [UI_SESSION_NOT_CONFIGURED]:
    "The UI server has no organization API key, so it cannot mint a browser session. Set SKYVERN_API_KEY on the UI server.",
  [UI_SESSION_UPSTREAM_UNREACHABLE]:
    "The UI server could not reach the Skyvern API to mint a browser session. Check SKYVERN_API_BASE_URL on the UI server and its network path to the API.",
  [UI_SESSION_UPSTREAM_REJECTED]:
    "The Skyvern API rejected the UI server's organization API key. Check that SKYVERN_API_KEY on the UI server matches a valid key for this deployment.",
  [UI_SESSION_UPSTREAM_INVALID]:
    "The Skyvern API returned an unexpected response to the session-token request. Check that SKYVERN_API_BASE_URL points at the Skyvern API and not at a proxy or the UI itself.",
};

function resolveOrganizationApiKey(env = process.env) {
  const key = env.SKYVERN_API_KEY?.trim() || env.VITE_SKYVERN_API_KEY?.trim();
  return key || null;
}

function isLoopbackUrl(value) {
  try {
    return LOOPBACK_HOSTNAMES.has(new URL(value).hostname);
  } catch {
    return false;
  }
}

function formatApiBaseUrlForLogging(value) {
  try {
    const url = new URL(value);
    url.username = "";
    url.password = "";
    url.search = "";
    url.hash = "";
    return url.toString();
  } catch {
    return "<invalid upstream URL>";
  }
}

function resolveServerApiBaseUrl(
  env = process.env,
  { isContainerized = existsSync("/.dockerenv") } = {},
) {
  const serverApiBaseUrl = env.SKYVERN_API_BASE_URL?.trim();
  if (serverApiBaseUrl) {
    return serverApiBaseUrl;
  }

  const browserApiBaseUrl =
    env.VITE_API_BASE_URL?.trim() || DEFAULT_API_BASE_URL;
  if (isContainerized && isLoopbackUrl(browserApiBaseUrl)) {
    return COMPOSE_API_BASE_URL;
  }
  return browserApiBaseUrl;
}

function loadEnvironmentFile(env, envFilePath) {
  let contents;
  try {
    contents = readFileSync(envFilePath, "utf8");
  } catch (error) {
    if (error?.code === "ENOENT") {
      return;
    }
    throw error;
  }

  for (const [key, value] of Object.entries(parseEnv(contents))) {
    if (!Object.hasOwn(env, key)) {
      env[key] = value;
    }
  }
}

function resolveLocalServerConfiguration({
  env = process.env,
  envFilePath = fileURLToPath(new URL(".env", import.meta.url)),
} = {}) {
  loadEnvironmentFile(env, envFilePath);
  return {
    apiBaseUrl: resolveServerApiBaseUrl(env),
    organizationApiKey: resolveOrganizationApiKey(env),
  };
}

function sendJson(response, statusCode, payload) {
  const body = JSON.stringify(payload);
  response.writeHead(statusCode, {
    "cache-control": "no-store",
    "content-length": Buffer.byteLength(body),
    "content-type": "application/json; charset=utf-8",
  });
  response.end(body);
}

// Distinct label so the nonce key is a one-way derivation and never doubles as the organization key.
const UI_SESSION_NONCE_KEY_LABEL = "skyvern.ui-session.nonce.v1";

function createUiSessionNonceManager(secret) {
  // Every replica must derive the same key: the nonce is issued by whichever pod served the HTML and
  // validated by whichever pod handles the mint, and the Service has no session affinity. A
  // per-process random key 403s that legitimate cross-pod pair, including during a rolling update.
  const signingKey = secret
    ? createHmac("sha256", String(secret))
        .update(UI_SESSION_NONCE_KEY_LABEL)
        .digest()
    : randomBytes(32);

  return {
    issue() {
      const nonce = randomBytes(32).toString("base64url");
      const signature = createHmac("sha256", signingKey)
        .update(nonce)
        .digest("base64url");
      return `${nonce}.${signature}`;
    },
    validate(value) {
      if (typeof value !== "string") {
        return false;
      }
      const separator = value.indexOf(".");
      if (separator < 1) {
        return false;
      }
      const nonce = value.slice(0, separator);
      const signature = Buffer.from(value.slice(separator + 1), "base64url");
      const expected = createHmac("sha256", signingKey).update(nonce).digest();
      return (
        signature.length === expected.length &&
        timingSafeEqual(signature, expected)
      );
    },
  };
}

function issueUiSessionNonceCookie(request, response, nonceManager) {
  if (
    request.method !== "GET" ||
    !request.headers.accept
      ?.split(",")
      .some((value) => value.trim().toLowerCase().startsWith("text/html"))
  ) {
    return;
  }
  response.setHeader(
    "set-cookie",
    `${UI_SESSION_NONCE_COOKIE}=${nonceManager.issue()}; Path=/ui-session; HttpOnly; SameSite=Strict`,
  );
}

function getCookie(request, name) {
  for (const part of request.headers.cookie?.split(";") ?? []) {
    const separator = part.indexOf("=");
    if (separator > 0 && part.slice(0, separator).trim() === name) {
      return part.slice(separator + 1).trim();
    }
  }
  return null;
}

function getRequestHost(request) {
  const host = request.headers.host;
  if (!host) {
    return null;
  }
  try {
    return new URL(`http://${host}`).host;
  } catch {
    return null;
  }
}

// Hosts, not full origins: a TLS-terminating proxy that forwards plain HTTP without
// x-forwarded-proto makes every same-origin request look like a scheme mismatch.
function hasCrossSiteSignal(request) {
  const requestHost = getRequestHost(request);
  for (const header of [request.headers.origin, request.headers.referer]) {
    if (!header) {
      continue;
    }
    try {
      if (!requestHost || new URL(header).host !== requestHost) {
        return true;
      }
    } catch {
      return true;
    }
  }
  return false;
}

// Sec-Fetch-Site is computed by the browser and cannot be set by page script, so it survives
// proxies that rewrite Host. The header comparison below cannot, so it is only the fallback
// for clients that omit Sec-Fetch-Site.
function isCrossSiteRequest(request) {
  const secFetchSite = request.headers["sec-fetch-site"];
  if (secFetchSite) {
    return secFetchSite !== "same-origin";
  }
  return hasCrossSiteSignal(request);
}

function isUiSessionResponse(value) {
  return (
    value !== null &&
    typeof value === "object" &&
    typeof value.token === "string" &&
    value.token.length > 0 &&
    typeof value.expires_at === "number" &&
    Number.isFinite(value.expires_at) &&
    value.expires_at * 1000 > Date.now()
  );
}

function mintFailure(reason) {
  const error = new Error(reason);
  error.reason = reason;
  return error;
}

function createUiSessionHandler({
  apiBaseUrl,
  organizationApiKey,
  fetchImpl = fetch,
  logger = console,
  requestTimeoutMs = UI_SESSION_REQUEST_TIMEOUT_MS,
  nonceManager = createUiSessionNonceManager(organizationApiKey),
} = {}) {
  let cachedUiSession = null;
  const loggedMintFailures = new Set();
  let mintInFlight = null;

  // Never interpolate the upstream error: its message can echo back a credentialed upstream URL.
  const logMintFailureOnce = (reason) => {
    if (loggedMintFailures.has(reason)) {
      return;
    }
    loggedMintFailures.add(reason);
    const loggableApiBaseUrl = formatApiBaseUrlForLogging(apiBaseUrl);
    logger.error(
      `UI session minting failed against ${loggableApiBaseUrl} (${reason}): ${UI_SESSION_FAILURE_DETAIL[reason]} ` +
        "Set SKYVERN_API_BASE_URL to an API address the UI server itself can reach — the browser-facing " +
        "VITE_API_BASE_URL is often routed through an external load balancer that the UI server cannot use.",
    );
  };

  const mintUiSession = async () => {
    const abortController = new AbortController();
    const timeoutId = setTimeout(
      () => abortController.abort(),
      requestTimeoutMs,
    );
    try {
      let upstream;
      try {
        upstream = await fetchImpl(
          `${apiBaseUrl.replace(/\/+$/, "")}/ui-session`,
          {
            method: "POST",
            headers: {
              accept: "application/json",
              "x-api-key": organizationApiKey,
            },
            signal: abortController.signal,
          },
        );
      } catch {
        throw mintFailure(UI_SESSION_UPSTREAM_UNREACHABLE);
      }
      if (!upstream.ok) {
        throw mintFailure(UI_SESSION_UPSTREAM_REJECTED);
      }
      let payload;
      try {
        payload = await upstream.json();
      } catch {
        throw mintFailure(UI_SESSION_UPSTREAM_INVALID);
      }
      if (!isUiSessionResponse(payload)) {
        throw mintFailure(UI_SESSION_UPSTREAM_INVALID);
      }
      return {
        token: payload.token,
        expires_at: payload.expires_at,
      };
    } finally {
      clearTimeout(timeoutId);
    }
  };

  const getUiSession = async (bypassCache) => {
    if (
      !bypassCache &&
      cachedUiSession &&
      cachedUiSession.expires_at * 1000 - Date.now() >
        UI_SESSION_CACHE_EXPIRY_BUFFER_MS
    ) {
      return cachedUiSession;
    }
    if (!mintInFlight) {
      mintInFlight = mintUiSession()
        .then((payload) => {
          cachedUiSession = payload;
          return payload;
        })
        .finally(() => {
          mintInFlight = null;
        });
    }
    return await mintInFlight;
  };

  return async (request, response) => {
    if (request.method !== "GET") {
      sendJson(response, 405, { detail: "Method not allowed" });
      return;
    }
    const nonce = getCookie(request, UI_SESSION_NONCE_COOKIE);
    if (isCrossSiteRequest(request) || !nonceManager.validate(nonce)) {
      sendJson(response, 403, {
        detail: "Cross-site requests are not allowed",
      });
      return;
    }
    const bypassCache =
      request.headers["x-skyvern-ui-session-refresh"] === "auth-failure";
    if (!organizationApiKey) {
      sendJson(response, 503, {
        detail: UI_SESSION_FAILURE_DETAIL[UI_SESSION_NOT_CONFIGURED],
        reason: UI_SESSION_NOT_CONFIGURED,
      });
      return;
    }

    try {
      sendJson(response, 200, await getUiSession(bypassCache));
    } catch (error) {
      const reason = UI_SESSION_FAILURE_DETAIL[error?.reason]
        ? error.reason
        : UI_SESSION_UPSTREAM_INVALID;
      logMintFailureOnce(reason);
      sendJson(response, 502, {
        detail: UI_SESSION_FAILURE_DETAIL[reason],
        reason,
      });
    }
  };
}

function createLocalServer({
  env = process.env,
  apiBaseUrl = resolveServerApiBaseUrl(env),
  organizationApiKey = resolveOrganizationApiKey(env),
  fetchImpl = fetch,
  logger = console,
  requestTimeoutMs = UI_SESSION_REQUEST_TIMEOUT_MS,
} = {}) {
  const nonceManager = createUiSessionNonceManager(organizationApiKey);
  const serveUiSession = createUiSessionHandler({
    apiBaseUrl,
    organizationApiKey,
    fetchImpl,
    logger,
    requestTimeoutMs,
    nonceManager,
  });

  return createServer(async (request, response) => {
    const requestUrl = new URL(request.url || "/", "http://localhost");
    if (requestUrl.pathname === "/ui-session") {
      await serveUiSession(request, response);
      return;
    }

    issueUiSessionNonceCookie(request, response, nonceManager);
    await handler(request, response, {
      public: "dist",
      rewrites: [
        {
          source: "**",
          destination: "/index.html",
        },
      ],
    });
  });
}

function startLocalServer() {
  const { apiBaseUrl, organizationApiKey } = resolveLocalServerConfiguration();
  if (organizationApiKey) {
    console.log("UI session minting is configured");
  } else {
    console.error("UI session minting is disabled: no organization key found");
  }
  const server = createLocalServer({ apiBaseUrl, organizationApiKey });
  server.listen(8080, async () => {
    console.log("Running at http://localhost:8080");
    try {
      await open("http://localhost:8080");
    } catch {
      // Expected to fail in Docker containers where no browser is available
    }
  });
}

const isMainModule =
  process.argv[1] !== undefined &&
  import.meta.url === pathToFileURL(process.argv[1]).href;

if (isMainModule) {
  startLocalServer();
}

export {
  createLocalServer,
  createUiSessionHandler,
  createUiSessionNonceManager,
  issueUiSessionNonceCookie,
  resolveLocalServerConfiguration,
  resolveOrganizationApiKey,
  resolveServerApiBaseUrl,
  startLocalServer,
};
