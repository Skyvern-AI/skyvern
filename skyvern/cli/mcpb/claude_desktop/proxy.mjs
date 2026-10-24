// @ts-check
import process from "node:process";
import { pathToFileURL } from "node:url";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/client";
import { StdioServerTransport } from "@modelcontextprotocol/server/stdio";
import { InitializeResultSchema } from "@modelcontextprotocol/core";

class BoundaryError extends Error {
  /** @param {string} code */
  constructor(code) {
    super(code);
    this.code = code;
  }
}

/** @typedef {{endpoint: URL, apiKey: string}} BridgeConfiguration */
/** @typedef {{stdin?: import('node:stream').Readable, stdout?: import('node:stream').Writable, fetchImpl?: typeof fetch, onFault?: (code: string) => void, maxInFlight?: number}} BridgeOptions */

/** Read only explicit API-key configuration; OAuth credentials are never loaded.
 * @param {Record<string, string | undefined>} env
 * @returns {BridgeConfiguration}
 */
export function readConfiguration(env) {
  let endpoint;
  try {
    endpoint = new URL(env.SKYVERN_MCP_URL ?? "https://api.skyvern.com/mcp/");
  } catch {
    throw new Error("CONFIG_INVALID_URL");
  }
  if (
    endpoint.protocol !== "https:" ||
    endpoint.username ||
    endpoint.password ||
    endpoint.hash
  ) {
    throw new Error("CONFIG_INVALID_URL");
  }
  const apiKey = env.SKYVERN_API_KEY;
  if (!apiKey || !/^[\x21-\x7e]+$/.test(apiKey))
    throw new Error("CONFIG_INVALID_KEY");
  return { endpoint, apiKey };
}

/** Enforce the configured origin on every request, regardless of SDK defaults.
 * @param {BridgeConfiguration} config
 * @param {typeof fetch} fetchImpl
 * @param {AbortSignal} shutdown
 * @param {{headerTimeoutMs?: number, maxActiveResponses?: number}} [options]
 * @returns {typeof fetch}
 */
export function createPinnedFetch(config, fetchImpl, shutdown, options = {}) {
  const origin = config.endpoint.origin;
  const timeout = options.headerTimeoutMs ?? 30_000;
  const limit = options.maxActiveResponses ?? 64;
  if (
    !Number.isSafeInteger(timeout) ||
    timeout < 1 ||
    timeout > 30_000 ||
    !Number.isSafeInteger(limit) ||
    limit < 1 ||
    limit > 64
  )
    throw new Error("CONFIG_INVALID_LIMIT");
  let active = 0;
  return async (input, init) => {
    let target;
    try {
      target = new URL(input instanceof Request ? input.url : String(input));
    } catch {
      throw new BoundaryError("REMOTE_ORIGIN_REFUSED");
    }
    if (
      target.origin !== origin ||
      target.username ||
      target.password ||
      target.hash
    ) {
      throw new BoundaryError("REMOTE_ORIGIN_REFUSED");
    }
    const headers = new Headers(
      init?.headers ?? (input instanceof Request ? input.headers : undefined),
    );
    headers.delete("Authorization");
    headers.delete("Proxy-Authorization");
    headers.delete("Cookie");
    headers.set("x-api-key", config.apiKey);
    const deadline = new AbortController();
    const signals = [shutdown, deadline.signal];
    if (init?.signal) signals.push(init.signal);
    if (input instanceof Request) signals.push(input.signal);
    const signal = AbortSignal.any(signals);
    if (signal.aborted) throw new DOMException("Request aborted", "AbortError");
    if (active >= limit) throw new BoundaryError("BRIDGE_BUSY");
    active++;
    let released = false;
    let removeAbort = () => {};
    const release = () => {
      if (released) return;
      released = true;
      active--;
      removeAbort();
    };
    const timer = setTimeout(
      () =>
        deadline.abort(
          new DOMException("Response headers timed out", "TimeoutError"),
        ),
      timeout,
    );
    let response;
    try {
      response = await fetchImpl(input, {
        ...init,
        headers,
        redirect: "error",
        credentials: "omit",
        signal,
      });
    } catch (error) {
      release();
      throw error;
    } finally {
      // A header deadline must never terminate a legitimate long-lived body.
      clearTimeout(timer);
    }
    try {
      if (
        response.redirected ||
        (response.status >= 300 && response.status < 400)
      )
        throw new BoundaryError("REMOTE_REDIRECT_REFUSED");
      if (response.url && new URL(response.url).origin !== origin)
        throw new BoundaryError("REMOTE_ORIGIN_REFUSED");
      if (response.status === 401)
        throw new BoundaryError("REMOTE_AUTH_REQUIRED");
      if (!response.body) {
        release();
        return response;
      }
      const reader = response.body.getReader();
      const body = new ReadableStream({
        start(controller) {
          const abort = () => {
            release();
            controller.error(new DOMException("Request aborted", "AbortError"));
            void reader.cancel().catch(() => {});
          };
          removeAbort = () => signal.removeEventListener("abort", abort);
          signal.addEventListener("abort", abort, { once: true });
          if (signal.aborted) abort();
        },
        async pull(controller) {
          try {
            const result = await reader.read();
            if (released) return;
            if (result.done) {
              release();
              controller.close();
            } else controller.enqueue(result.value);
          } catch (error) {
            if (!released) {
              release();
              controller.error(error);
            }
          }
        },
        async cancel(reason) {
          release();
          await reader.cancel(reason);
        },
      });
      const wrapped = new Response(body, {
        status: response.status,
        statusText: response.statusText,
        headers: response.headers,
      });
      for (const property of ["url", "type", "redirected"])
        Object.defineProperty(wrapped, property, {
          value: Reflect.get(response, property),
        });
      return wrapped;
    } catch (error) {
      release();
      await response.body?.cancel().catch(() => {});
      throw error;
    }
  };
}

/** Forward MCP frames using the SDK transports without an OAuth provider.
 * @param {BridgeConfiguration} configuration
 * @param {BridgeOptions} [options]
 */
export async function startBridge(configuration, options = {}) {
  const config = readConfiguration({
    SKYVERN_MCP_URL: configuration.endpoint.href,
    SKYVERN_API_KEY: configuration.apiKey,
  });
  const input = options.stdin ?? process.stdin;
  const output = options.stdout ?? process.stdout;
  const limit = options.maxInFlight ?? 64;
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > 64)
    throw new Error("CONFIG_INVALID_LIMIT");
  const shutdown = new AbortController();
  const remote = new StreamableHTTPClientTransport(config.endpoint, {
    fetch: createPinnedFetch(
      config,
      options.fetchImpl ?? globalThis.fetch,
      shutdown.signal,
      { maxActiveResponses: limit },
    ),
    requestInit: { headers: { "x-api-key": config.apiKey } },
  });
  const local = new StdioServerTransport(input, output, {
    maxBufferSize: 10 * 1024 * 1024,
  });
  let closing = false;
  let inFlight = 0;
  /** @type {string | number | undefined} */
  let initializationId;
  /** @type {Promise<void> | undefined} */
  let closingPromise;
  /** @type {(code: number) => void} */
  let finish = () => {};
  /** @type {Promise<number>} */
  const done = new Promise((resolve) => {
    finish = resolve;
  });
  /** @param {string} code */
  function report(code) {
    try {
      options.onFault?.(code);
    } catch {
      /* Cleanup still owns completion. */
    }
  }
  const onEnd = () => {
    void close();
  };
  /** @param {number} [code] @returns {Promise<void>} */
  function close(code = 0) {
    if (closing) return closingPromise ?? Promise.resolve();
    closing = true;
    shutdown.abort();
    input.off("end", onEnd);
    input.off("close", onEnd);
    closingPromise = Promise.allSettled([
      Promise.resolve().then(() => remote.close()),
      Promise.resolve().then(() => local.close()),
    ]).then((results) => {
      const failed = results.some((result) => result.status === "rejected");
      if (failed) report("CLEANUP_FAILED");
      finish(failed ? 1 : code);
    });
    return closingPromise;
  }
  /** @param {string} code */
  function fail(code) {
    if (!closing) {
      report(code);
      void close(1);
    }
  }
  /** @param {() => Promise<void>} operation @param {string} code */
  function forward(operation, code) {
    if (closing) return;
    if (inFlight >= limit) {
      fail("BRIDGE_BUSY");
      return;
    }
    inFlight++;
    void Promise.resolve()
      .then(() => {
        if (!closing) return operation();
      })
      .catch((error) =>
        fail(error instanceof BoundaryError ? error.code : code),
      )
      .finally(() => {
        inFlight--;
      });
  }
  local.onmessage = (message) => {
    // This adapter exposes the legacy initialize handshake of its HTTP peer.
    // Decline modern discovery on stdio so clients can fall back without
    // sending an unsupported pre-initialize request to Skyvern's endpoint.
    if (
      "method" in message &&
      message.method === "server/discover" &&
      "id" in message
    ) {
      forward(
        () =>
          local.send({
            jsonrpc: "2.0",
            id: message.id,
            error: { code: -32601, message: "Method not found" },
          }),
        "STDIO_IO_FAILED",
      );
      return;
    }
    if (
      "method" in message &&
      message.method === "initialize" &&
      "id" in message
    ) {
      if (initializationId !== undefined) {
        fail("PROTOCOL_INIT_PENDING");
        return;
      }
      initializationId = message.id;
    }
    forward(() => remote.send(message), "REMOTE_IO_FAILED");
  };
  remote.onmessage = (message) => {
    if (
      initializationId !== undefined &&
      "id" in message &&
      message.id === initializationId &&
      ("result" in message || "error" in message)
    ) {
      initializationId = undefined;
      if ("result" in message) {
        const result = InitializeResultSchema.safeParse(message.result);
        if (
          !result.success ||
          !/^\d{4}-\d{2}-\d{2}$/.test(result.data.protocolVersion)
        ) {
          fail("PROTOCOL_VERSION_REFUSED");
          return;
        }
        try {
          remote.setProtocolVersion(result.data.protocolVersion);
        } catch {
          fail("PROTOCOL_VERSION_REFUSED");
          return;
        }
      }
    }
    forward(() => local.send(message), "STDIO_IO_FAILED");
  };
  local.onerror = () => fail("STDIO_IO_FAILED");
  remote.onerror = (error) =>
    fail(error instanceof BoundaryError ? error.code : "REMOTE_IO_FAILED");
  local.onclose = () => {
    void close();
  };
  remote.onclose = () => fail("REMOTE_CLOSED");
  input.once("end", onEnd);
  input.once("close", onEnd);
  try {
    await remote.start();
    if (!closing) await local.start();
  } catch {
    await close(1);
    throw new Error("BRIDGE_START_FAILED");
  }
  return { done, close };
}

/** Run the configured bundle entrypoint. */
export async function main() {
  let bridge;
  try {
    if (process.argv.length > 2)
      throw new Error("CONFIG_UNSUPPORTED_ARGUMENTS");
    bridge = await startBridge(readConfiguration(process.env), {
      onFault: (code) => {
        process.stderr.write(`Skyvern API-key fallback stopped (${code}).\n`);
      },
    });
  } catch {
    process.stderr.write(
      "Skyvern API-key fallback configuration/start failed.\n",
    );
    return 1;
  }
  const stop = () => {
    void bridge.close();
  };
  process.once("SIGINT", stop);
  process.once("SIGTERM", stop);
  try {
    return await bridge.done;
  } finally {
    process.off("SIGINT", stop);
    process.off("SIGTERM", stop);
  }
}

if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(process.argv[1]).href
) {
  process.exitCode = await main();
}
