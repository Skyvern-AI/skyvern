import { AxiosError, type AxiosAdapter, type AxiosResponse } from "axios";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const API_KEY_STORAGE_KEY = "skyvern.apiKey";
const API_KEY_EXPIRES_AT_STORAGE_KEY = "skyvern.apiKeyExpiresAt";

function createAuthRetryAdapter() {
  const adapter = vi.fn<AxiosAdapter>(async (config) => {
    const response: AxiosResponse = {
      data: { ok: true },
      status: 200,
      statusText: "OK",
      headers: {},
      config,
    };
    if (adapter.mock.calls.length === 1) {
      throw new AxiosError(
        "Request failed",
        AxiosError.ERR_BAD_REQUEST,
        config,
        undefined,
        {
          ...response,
          data: { detail: "Invalid credentials" },
          status: 401,
          statusText: "Unauthorized",
        },
      );
    }
    return response;
  });
  return adapter;
}

describe("UI session refresh", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.useFakeTimers({ now: new Date("2030-01-01T00:00:00Z") });
    window.sessionStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    window.sessionStorage.clear();
  });

  function htmlCatchAll() {
    // What an SPA catch-all or a proxy mid-rollout returns for a path that does exist.
    return new Response("<!doctype html>", {
      status: 200,
      headers: { "content-type": "text/html" },
    });
  }

  function mintedSession() {
    return new Response(
      JSON.stringify({
        token: "recovered-session-canary",
        expires_at: Math.floor(Date.now() / 1000) + 3600,
      }),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  }

  it("still mints after a single non-JSON response from an endpoint that exists", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(htmlCatchAll())
      .mockResolvedValue(mintedSession());
    vi.stubGlobal("fetch", fetchMock);

    const { initializeUiSession } = await import("./AxiosClient");
    await initializeUiSession();
    await initializeUiSession();

    // The old latch disabled refresh permanently on that first response, so no token was ever stored.
    expect(window.sessionStorage.getItem(API_KEY_STORAGE_KEY)).toBe(
      "recovered-session-canary",
    );
  });

  it("gives up probing an endpoint that is genuinely absent", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response("not found", { status: 404 }));
    vi.stubGlobal("fetch", fetchMock);

    const { initializeUiSession } = await import("./AxiosClient");
    for (let attempt = 0; attempt < 4; attempt += 1) {
      await initializeUiSession();
    }
    const afterLatch = fetchMock.mock.calls.length;
    await initializeUiSession();
    await initializeUiSession();

    expect(window.sessionStorage.getItem(API_KEY_STORAGE_KEY)).toBeNull();
    expect(fetchMock.mock.calls.length).toBe(afterLatch);
  });

  it("schedules a refresh for a persisted unexpired UI session", async () => {
    const now = Math.floor(Date.now() / 1000);
    window.sessionStorage.setItem(
      API_KEY_STORAGE_KEY,
      "persisted-ui-session-canary",
    );
    window.sessionStorage.setItem(
      API_KEY_EXPIRES_AT_STORAGE_KEY,
      String(now + 120),
    );
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          token: "scheduled-refresh-canary",
          expires_at: now + 240,
        }),
        {
          status: 200,
          headers: { "content-type": "application/json" },
        },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const { initializeUiSession } = await import("./AxiosClient");
    await initializeUiSession();

    expect(fetchMock).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(59_999);
    expect(fetchMock).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["missing", null],
    ["expired", -1],
  ])(
    "mints on initialization when the persisted expiry is %s",
    async (_state, expiryOffset) => {
      const now = Math.floor(Date.now() / 1000);
      window.sessionStorage.setItem(
        API_KEY_STORAGE_KEY,
        "stale-ui-session-canary",
      );
      if (expiryOffset !== null) {
        window.sessionStorage.setItem(
          API_KEY_EXPIRES_AT_STORAGE_KEY,
          String(now + expiryOffset),
        );
      }
      const fetchMock = vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            token: "replacement-ui-session-canary",
            expires_at: now + 120,
          }),
          {
            status: 200,
            headers: { "content-type": "application/json" },
          },
        ),
      );
      vi.stubGlobal("fetch", fetchMock);

      const { initializeUiSession } = await import("./AxiosClient");
      await initializeUiSession();

      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(window.sessionStorage.getItem(API_KEY_STORAGE_KEY)).toBe(
        "replacement-ui-session-canary",
      );
      expect(
        window.sessionStorage.getItem(API_KEY_EXPIRES_AT_STORAGE_KEY),
      ).toBe(String(now + 120));
    },
  );

  it("mints on initialization when no token is persisted", async () => {
    const now = Math.floor(Date.now() / 1000);
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          token: "initial-ui-session-canary",
          expires_at: now + 120,
        }),
        {
          status: 200,
          headers: { "content-type": "application/json" },
        },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const { initializeUiSession } = await import("./AxiosClient");
    await initializeUiSession();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(window.sessionStorage.getItem(API_KEY_STORAGE_KEY)).toBe(
      "initial-ui-session-canary",
    );
    expect(window.sessionStorage.getItem(API_KEY_EXPIRES_AT_STORAGE_KEY)).toBe(
      String(now + 120),
    );
  });

  it("aborts a hung session request and completes initialization", async () => {
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener(
          "abort",
          () => reject(init.signal?.reason),
          { once: true },
        );
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    const { initializeUiSession } = await import("./AxiosClient");
    let initialized = false;
    const initialization = initializeUiSession().then(() => {
      initialized = true;
    });

    await vi.advanceTimersByTimeAsync(9_999);
    expect(initialized).toBe(false);

    await vi.advanceTimersByTimeAsync(1);
    await expect(initialization).resolves.toBeUndefined();
    expect(fetchMock.mock.calls[0]?.[1]?.signal?.aborted).toBe(true);
  });

  it("retries minting after a transient UI session failure", async () => {
    const now = Math.floor(Date.now() / 1000);
    const recoveredToken = "recovered-ui-session-canary";
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("Network unavailable"))
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            token: recoveredToken,
            expires_at: now + 120,
          }),
          {
            status: 200,
            headers: { "content-type": "application/json" },
          },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    const { getClient, initializeUiSession } = await import("./AxiosClient");
    await initializeUiSession();
    const client = await getClient(null);
    const adapter = createAuthRetryAdapter();
    client.defaults.adapter = adapter;

    await expect(client.get("/protected")).resolves.toMatchObject({
      data: { ok: true },
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(adapter).toHaveBeenCalledTimes(2);
    expect(adapter.mock.calls[1]?.[0].headers.get("X-API-Key")).toBe(
      recoveredToken,
    );
    expect(window.sessionStorage.getItem(API_KEY_STORAGE_KEY)).toBe(
      recoveredToken,
    );
  });

  it("retries initialization after a transient UI session failure", async () => {
    const now = Math.floor(Date.now() / 1000);
    const recoveredToken = "reinitialized-ui-session-canary";
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: "Temporarily unavailable" }), {
          status: 502,
          headers: { "content-type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            token: recoveredToken,
            expires_at: now + 120,
          }),
          {
            status: 200,
            headers: { "content-type": "application/json" },
          },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    const { initializeUiSession } = await import("./AxiosClient");
    await initializeUiSession();
    await initializeUiSession();

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(window.sessionStorage.getItem(API_KEY_STORAGE_KEY)).toBe(
      recoveredToken,
    );
  });

  it.each([
    [
      "404 response",
      () =>
        new Response(JSON.stringify({ detail: "Not found" }), {
          status: 404,
          headers: { "content-type": "application/json" },
        }),
    ],
    [
      "non-JSON response",
      () =>
        new Response("<html>Not found</html>", {
          status: 200,
          headers: { "content-type": "text/html" },
        }),
    ],
  ])(
    "disables minting when the UI session endpoint returns a %s",
    async (_responseType, createResponse) => {
      const fetchMock = vi.fn().mockResolvedValue(createResponse());
      vi.stubGlobal("fetch", fetchMock);

      const { getClient, initializeUiSession } = await import("./AxiosClient");
      // One such response is ambiguous — a proxy mid-rollout produces it for an endpoint that
      // exists — so the latch needs a run of them before concluding the endpoint is absent.
      for (let attempt = 0; attempt < 4; attempt += 1) {
        await initializeUiSession();
      }
      const mintsBeforeAuthFailure = fetchMock.mock.calls.length;

      const client = await getClient(null);
      const adapter = createAuthRetryAdapter();
      client.defaults.adapter = adapter;

      await expect(client.get("/protected")).rejects.toMatchObject({
        response: { status: 401 },
      });
      // A 401 buys exactly one re-probe, because it is evidence the deployment does want a token.
      const mintsAfterAuthFailure = fetchMock.mock.calls.length;
      expect(mintsAfterAuthFailure).toBe(mintsBeforeAuthFailure + 1);
      expect(adapter).toHaveBeenCalledTimes(1);

      // Every later failure is silent: no unbounded probing of an absent endpoint.
      const secondAdapter = createAuthRetryAdapter();
      client.defaults.adapter = secondAdapter;
      await expect(client.get("/protected")).rejects.toMatchObject({
        response: { status: 401 },
      });
      expect(fetchMock.mock.calls.length).toBe(mintsAfterAuthFailure);
    },
  );

  it("treats a valid persisted UI session as endpoint confirmation", async () => {
    const now = Math.floor(Date.now() / 1000);
    const recoveredToken = "persisted-recovery-ui-session-canary";
    window.sessionStorage.setItem(
      API_KEY_STORAGE_KEY,
      "persisted-ui-session-canary",
    );
    window.sessionStorage.setItem(
      API_KEY_EXPIRES_AT_STORAGE_KEY,
      String(now + 120),
    );
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: "Not found" }), {
          status: 404,
          headers: { "content-type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            token: recoveredToken,
            expires_at: now + 240,
          }),
          {
            status: 200,
            headers: { "content-type": "application/json" },
          },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    const { getClient, initializeUiSession } = await import("./AxiosClient");
    await initializeUiSession();
    expect(fetchMock).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(60_000);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    const client = await getClient(null);
    const adapter = createAuthRetryAdapter();
    client.defaults.adapter = adapter;

    await expect(client.get("/protected")).resolves.toMatchObject({
      data: { ok: true },
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(adapter).toHaveBeenCalledTimes(2);
    expect(window.sessionStorage.getItem(API_KEY_STORAGE_KEY)).toBe(
      recoveredToken,
    );
  });

  it("refreshes after an auth failure, retries once, and succeeds", async () => {
    const expiredToken = "expired-ui-session-canary";
    const refreshedToken = "refreshed-ui-session-canary";
    window.sessionStorage.setItem(API_KEY_STORAGE_KEY, expiredToken);
    window.sessionStorage.setItem(
      API_KEY_EXPIRES_AT_STORAGE_KEY,
      String(Math.floor(Date.now() / 1000) + 120),
    );
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          token: refreshedToken,
          expires_at: Math.floor(Date.now() / 1000) + 120,
        }),
        {
          status: 200,
          headers: { "content-type": "application/json" },
        },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const { getClient, initializeUiSession } = await import("./AxiosClient");
    await initializeUiSession();
    const client = await getClient(null);
    const adapter = createAuthRetryAdapter();
    client.defaults.adapter = adapter;

    const response = await client.get("/protected");

    expect(response.data).toEqual({ ok: true });
    expect(adapter).toHaveBeenCalledTimes(2);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(
      new Headers(fetchMock.mock.calls[0]?.[1]?.headers).get(
        "x-skyvern-ui-session-refresh",
      ),
    ).toBe("auth-failure");
    expect(adapter.mock.calls[1]?.[0].headers.get("X-API-Key")).toBe(
      refreshedToken,
    );
    expect(window.sessionStorage.getItem(API_KEY_STORAGE_KEY)).toBe(
      refreshedToken,
    );
  });

  it("holds the first request until the session token is minted", async () => {
    const token = "boot-race-session-canary";
    let releaseMint: (response: Response) => void = () => {};
    const mint = new Promise<Response>((resolve) => {
      releaseMint = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(() => mint),
    );

    const { getClient, initializeUiSession } = await import("./AxiosClient");
    // The entrypoints deliberately do not await this — they render first.
    void initializeUiSession();

    const clientPromise = getClient(null);
    let settled = false;
    void clientPromise.then(() => {
      settled = true;
    });
    await Promise.resolve();
    expect(settled).toBe(false);

    releaseMint(
      new Response(
        JSON.stringify({
          token,
          expires_at: Math.floor(Date.now() / 1000) + 3600,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    const client = await clientPromise;

    expect(client.defaults.headers.common["X-API-Key"]).toBe(token);
  });

  it("clears the unauthorized banner once a request succeeds", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(htmlCatchAll()));

    const { getClient } = await import("./AxiosClient");
    const { useAuthIssueStore } = await import("@/store/AuthIssueStore");
    useAuthIssueStore.getState().reportAuthIssue({
      statusCode: 403,
      detail: "Invalid authentication method",
      path: "/workflows",
    });

    const client = await getClient(null);
    client.defaults.adapter = vi.fn<AxiosAdapter>(async (config) => ({
      data: [],
      status: 200,
      statusText: "OK",
      headers: {},
      config,
    }));
    await client.get("/workflows");

    // A boot-race 403 otherwise pinned this banner for the life of the tab.
    expect(useAuthIssueStore.getState().issue).toBeNull();
  });

  it("reports why the UI server refused to mint a session", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            detail:
              "The UI server could not reach the Skyvern API to mint a browser session. Check SKYVERN_API_BASE_URL on the UI server and its network path to the API.",
            reason: "upstream_unreachable",
          }),
          { status: 502, headers: { "content-type": "application/json" } },
        ),
      ),
    );

    const { initializeUiSession } = await import("./AxiosClient");
    const { useAuthIssueStore } = await import("@/store/AuthIssueStore");
    await initializeUiSession();

    expect(useAuthIssueStore.getState().uiSessionFailure).toMatchObject({
      statusCode: 502,
      detail: expect.stringContaining("SKYVERN_API_BASE_URL"),
    });
  });

  it("does not report a deployment that simply has no mint endpoint", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(htmlCatchAll()));

    const { initializeUiSession } = await import("./AxiosClient");
    const { useAuthIssueStore } = await import("@/store/AuthIssueStore");
    await initializeUiSession();

    expect(useAuthIssueStore.getState().uiSessionFailure).toBeNull();
  });
});

describe("request-scoped authentication", () => {
  beforeEach(() => {
    vi.resetModules();
    window.sessionStorage.clear();
  });

  afterEach(() => {
    window.sessionStorage.clear();
  });

  it("keeps the credential snapshot after shared client defaults change", async () => {
    const { getClient, getClientWithRequestHeaders, setApiKeyHeader } =
      await import("./AxiosClient");
    setApiKeyHeader("api-key-a");
    const requestA = await getClientWithRequestHeaders(async () => "token-a");
    setApiKeyHeader("api-key-b");
    await getClient(async () => "token-b");
    const adapter = vi.fn<AxiosAdapter>(async (config) => ({
      data: { ok: true },
      status: 200,
      statusText: "OK",
      headers: {},
      config,
    }));
    requestA.client.defaults.adapter = adapter;

    await requestA.client.get("/scoped", { headers: requestA.headers });

    const headers = adapter.mock.calls[0]?.[0].headers;
    expect(headers?.get("Authorization")).toBe("Bearer token-a");
    expect(headers?.get("X-API-Key")).toBe("api-key-a");
  });
  it("waits for the runtime API key before snapshotting request headers", async () => {
    const token = "scoped-boot-race-session-canary";
    let releaseMint: (response: Response) => void = () => {};
    const mint = new Promise<Response>((resolve) => {
      releaseMint = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(() => mint),
    );

    const { getClientWithRequestHeaders, initializeUiSession } =
      await import("./AxiosClient");
    void initializeUiSession();

    const requestPromise = getClientWithRequestHeaders(async () => null);

    releaseMint(
      new Response(
        JSON.stringify({
          token,
          expires_at: Math.floor(Date.now() / 1000) + 3600,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    const request = await requestPromise;

    expect(request.headers.get("X-API-Key")).toBe(token);
  });
});

describe("open-source application entrypoint", () => {
  beforeEach(() => {
    vi.resetModules();
    window.sessionStorage.clear();
  });

  afterEach(() => {
    vi.doUnmock("../App.tsx");
    vi.doUnmock("react-dom/client");
    vi.unstubAllGlobals();
    window.sessionStorage.clear();
  });

  it("requests a UI session during startup", async () => {
    const render = vi.fn();
    vi.doMock("../App.tsx", () => ({ default: () => null }));
    vi.doMock("react-dom/client", () => ({
      default: {
        createRoot: () => ({ render }),
      },
    }));
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "Not configured" }), {
        status: 404,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await import("../main");

    expect(fetchMock).toHaveBeenCalledWith(
      "/ui-session",
      expect.objectContaining({
        cache: "no-store",
        headers: { accept: "application/json" },
      }),
    );
    expect(render).toHaveBeenCalledTimes(1);
  });
});
