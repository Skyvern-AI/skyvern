import test from "node:test";
import assert from "node:assert/strict";
import { PassThrough, Writable } from "node:stream";
import { once } from "node:events";
import {
  readConfiguration,
  createPinnedFetch,
  startBridge,
} from "../proxy.mjs";

const endpoint = "https://skyvern-fixture.invalid/mcp/";
const key = "synthetic-key-ONLY";
const config = () =>
  readConfiguration({ SKYVERN_MCP_URL: endpoint, SKYVERN_API_KEY: key });
const initMessage = {
  jsonrpc: "2.0",
  id: 1,
  method: "initialize",
  params: {
    protocolVersion: "2025-11-25",
    capabilities: {},
    clientInfo: { name: "local-fixture", version: "1" },
  },
};
const response = (id, result = { ok: true }) =>
  new Response(JSON.stringify({ jsonrpc: "2.0", id, result }), {
    headers: { "content-type": "application/json" },
  });
const initialized = () =>
  response(1, {
    protocolVersion: "2025-11-25",
    capabilities: {},
    serverInfo: { name: "fixture", version: "1" },
  });
const tick = () => new Promise((resolve) => setImmediate(resolve));

function outputCollector(output) {
  let buffer = "";
  const frames = [];
  const waiters = [];
  output.on("data", (chunk) => {
    buffer += chunk.toString();
    for (let at; (at = buffer.indexOf("\n")) >= 0;) {
      const frame = JSON.parse(buffer.slice(0, at));
      buffer = buffer.slice(at + 1);
      frames.push(frame);
      for (const waiter of [...waiters])
        if (waiter.accept(frame)) {
          waiters.splice(waiters.indexOf(waiter), 1);
          waiter.resolve(frame);
        }
    }
  });
  return {
    frames,
    waitFor(accept) {
      const present = frames.find(accept);
      if (present) return Promise.resolve(present);
      return new Promise((resolve) => waiters.push({ accept, resolve }));
    },
  };
}
async function harness(fetchImpl, options = {}) {
  const input = new PassThrough();
  const output = new PassThrough();
  const faults = [];
  const collector = outputCollector(output);
  const bridge = await startBridge(config(), {
    stdin: input,
    stdout: output,
    fetchImpl,
    onFault: (code) => faults.push(code),
    ...options,
  });
  return {
    input,
    output,
    faults,
    ...collector,
    ...bridge,
    send(message) {
      input.write(JSON.stringify(message) + "\n");
    },
  };
}

test("configuration requires HTTPS, no URL credentials/fragments and a header-safe key", () => {
  assert.equal(config().endpoint.href, endpoint);
  for (const url of [
    "http://skyvern-fixture.invalid/mcp",
    "https://user:secret@skyvern-fixture.invalid/mcp",
    "https://skyvern-fixture.invalid/mcp#part",
    "not a URL",
  ]) {
    assert.throws(
      () => readConfiguration({ SKYVERN_MCP_URL: url, SKYVERN_API_KEY: key }),
      /^Error: CONFIG_INVALID_URL$/,
    );
  }
  for (const apiKey of [
    "",
    " ",
    "value\nheader",
    "value\rheader",
    "unicode-ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã¢â‚¬Â¹Ãƒâ€¦Ã¢â‚¬Å“ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢",
  ]) {
    assert.throws(
      () => readConfiguration({ SKYVERN_API_KEY: apiKey }),
      /^Error: CONFIG_INVALID_KEY$/,
    );
  }
});

test("pinned fetch sets API key, refuses redirect semantics and removes unrelated credentials", async () => {
  const calls = [];
  const fetcher = createPinnedFetch(
    config(),
    async (url, init) => {
      calls.push({ url, init });
      return response(7);
    },
    new AbortController().signal,
  );
  await fetcher(endpoint, {
    method: "POST",
    redirect: "follow",
    credentials: "include",
    headers: {
      Authorization: "Bearer unrelated-secret",
      Cookie: "unrelated-cookie",
      "Proxy-Authorization": "unrelated-proxy",
      "x-api-key": "wrong",
    },
  });
  assert.equal(calls.length, 1);
  const { init } = calls[0];
  assert.equal(init.redirect, "error");
  assert.equal(init.credentials, "omit");
  assert.equal(init.headers.get("x-api-key"), key);
  for (const header of ["authorization", "cookie", "proxy-authorization"])
    assert.equal(init.headers.has(header), false);
});

test("other origins and userinfo are refused before fetch is called", async () => {
  let calls = 0;
  const fetcher = createPinnedFetch(
    config(),
    async () => {
      calls++;
      return response(7);
    },
    new AbortController().signal,
  );
  for (const url of [
    "https://other-fixture.invalid/mcp",
    "http://skyvern-fixture.invalid/mcp",
    "https://user@skyvern-fixture.invalid/mcp",
  ]) {
    await assert.rejects(fetcher(url), /REMOTE_ORIGIN_REFUSED/);
  }
  assert.equal(calls, 0);
});

test("even a supplied fetch returning a 3xx cannot turn it into another request", async () => {
  let calls = 0;
  const fetcher = createPinnedFetch(
    config(),
    async () => {
      calls++;
      return new Response(null, { status: 302 });
    },
    new AbortController().signal,
  );
  await assert.rejects(fetcher(endpoint), /REMOTE_REDIRECT_REFUSED/);
  assert.equal(calls, 1);
});

test("shutdown and the caller signal both reach fetch", async () => {
  const stop = new AbortController();
  const caller = new AbortController();
  let signal;
  const fetcher = createPinnedFetch(
    config(),
    async (_url, init) => {
      signal = init.signal;
      return response(1);
    },
    stop.signal,
  );
  await fetcher(endpoint, { signal: caller.signal });
  assert.equal(signal.aborted, false);
  caller.abort();
  assert.equal(signal.aborted, true);
  const stop2 = new AbortController();
  const fetcher2 = createPinnedFetch(
    config(),
    async (_url, init) => {
      signal = init.signal;
      return response(1);
    },
    stop2.signal,
  );
  await fetcher2(endpoint);
  stop2.abort();
  assert.equal(signal.aborted, true);
});

test(
  "no eager discovery; initialize and notifications pass through SDK transports",
  { timeout: 5000 },
  async () => {
    const calls = [];
    const h = await harness(async (url, init) => {
      calls.push({ url, init });
      if (init.method === "GET") return new Response(null, { status: 405 });
      const message = JSON.parse(init.body);
      return message.id === undefined
        ? new Response(null, { status: 202 })
        : initialized();
    });
    try {
      await tick();
      assert.equal(calls.length, 0);
      h.send(initMessage);
      const result = await h.waitFor((x) => x.id === 1);
      assert.equal(result.result.serverInfo.name, "fixture");
      h.send({ jsonrpc: "2.0", method: "notifications/initialized" });
      await tick();
      assert.ok(
        calls.some(
          (x) =>
            x.init.body &&
            JSON.parse(x.init.body).method === "notifications/initialized",
        ),
      );
      for (const call of calls) {
        assert.equal(call.init.headers.get("x-api-key"), key);
        assert.equal(call.init.redirect, "error");
      }
      assert.deepEqual(h.faults, []);
    } finally {
      await h.close();
    }
    assert.equal(await h.done, 0);
  },
);

test(
  "a legitimate JSON-RPC error is forwarded without rewriting it",
  { timeout: 5000 },
  async () => {
    const expected = {
      jsonrpc: "2.0",
      id: 5,
      error: { code: -32601, message: "Unknown method" },
    };
    const h = await harness(
      async () =>
        new Response(JSON.stringify(expected), {
          headers: { "content-type": "application/json" },
        }),
    );
    try {
      h.send({ jsonrpc: "2.0", id: 5, method: "fixture/unknown" });
      assert.deepEqual(await h.waitFor((x) => x.id === 5), expected);
    } finally {
      await h.close();
    }
  },
);

test(
  "401 fails closed without OAuth discovery, retry or credential-store use",
  { timeout: 5000 },
  async () => {
    const calls = [];
    const h = await harness(async (url, init) => {
      calls.push({ url, init });
      return new Response(null, {
        status: 401,
        headers: { "www-authenticate": "Bearer" },
      });
    });
    h.send(initMessage);
    assert.equal(await h.done, 1);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url.toString(), endpoint);
    assert.deepEqual(h.faults, ["REMOTE_AUTH_REQUIRED"]);
  },
);

test(
  "backend failures cannot print the credential or remote error detail",
  { timeout: 5000 },
  async () => {
    const h = await harness(async () => {
      throw new Error("remote detail " + key);
    });
    h.send(initMessage);
    assert.equal(await h.done, 1);
    assert.deepEqual(h.faults, ["REMOTE_IO_FAILED"]);
    assert.ok(!JSON.stringify(h.faults).includes(key));
    assert.equal(h.frames.length, 0);
  },
);

test(
  "EOF aborts an in-flight request and closes once without an automatic retry",
  { timeout: 5000 },
  async () => {
    let requests = 0;
    let observed;
    const started = new Promise((resolve) => {
      observed = resolve;
    });
    const h = await harness(async (_url, init) => {
      requests++;
      observed();
      return new Promise((_resolve, reject) =>
        init.signal.addEventListener(
          "abort",
          () => reject(new Error("aborted")),
          { once: true },
        ),
      );
    });
    h.send(initMessage);
    await started;
    h.input.end();
    assert.equal(await h.done, 0);
    await h.close();
    assert.equal(requests, 1);
    assert.deepEqual(h.faults, []);
  },
);

test(
  "bounded in-flight work fails closed rather than building an unbounded queue",
  { timeout: 5000 },
  async () => {
    let requests = 0;
    const h = await harness(
      async (_url, init) => {
        requests++;
        return new Promise((_resolve, reject) =>
          init.signal.addEventListener(
            "abort",
            () => reject(new Error("aborted")),
            { once: true },
          ),
        );
      },
      { maxInFlight: 1 },
    );
    h.send(initMessage);
    await tick();
    h.send({ jsonrpc: "2.0", id: 2, method: "ping" });
    assert.equal(await h.done, 1);
    assert.equal(requests, 1);
    assert.deepEqual(h.faults, ["BRIDGE_BUSY"]);
  },
);

test(
  "server-initiated messages and client replies do not deadlock behind an open SSE response",
  { timeout: 5000 },
  async () => {
    let controller;
    let replyReceived;
    const replied = new Promise((resolve) => {
      replyReceived = resolve;
    });
    const encoder = new TextEncoder();
    const h = await harness(async (_url, init) => {
      const message = JSON.parse(init.body);
      if (message.id === "server-question" && !message.method) {
        replyReceived();
        return new Response(null, { status: 202 });
      }
      return new Response(
        new ReadableStream({
          start(c) {
            controller = c;
            c.enqueue(
              encoder.encode(
                "event: message\ndata: " +
                  JSON.stringify({
                    jsonrpc: "2.0",
                    id: "server-question",
                    method: "ping",
                  }) +
                  "\n\n",
              ),
            );
          },
        }),
        { headers: { "content-type": "text/event-stream" } },
      );
    });
    try {
      h.send({ jsonrpc: "2.0", id: 9, method: "ping" });
      await h.waitFor((x) => x.id === "server-question");
      h.send({ jsonrpc: "2.0", id: "server-question", result: {} });
      await replied;
      controller.enqueue(
        encoder.encode(
          "event: message\ndata: " +
            JSON.stringify({ jsonrpc: "2.0", id: 9, result: {} }) +
            "\n\n",
        ),
      );
      controller.close();
      assert.deepEqual((await h.waitFor((x) => x.id === 9)).result, {});
    } finally {
      await h.close();
    }
  },
);

test(
  "malformed stdio input closes with a named error",
  { timeout: 5000 },
  async () => {
    let calls = 0;
    const h = await harness(async () => {
      calls++;
      return initialized();
    });
    h.input.write('{"wrong":"shape"}\n');
    assert.equal(await h.done, 1);
    assert.equal(calls, 0);
    assert.deepEqual(h.faults, ["STDIO_IO_FAILED"]);
  },
);

test(
  "stdout errors are contained and cannot expose response or key details",
  { timeout: 5000 },
  async () => {
    const faults = [];
    const input = new PassThrough();
    const output = new Writable({
      write(_chunk, _encoding, callback) {
        callback(new Error("private detail " + key));
      },
    });
    const h = await startBridge(config(), {
      stdin: input,
      stdout: output,
      fetchImpl: async () => initialized(),
      onFault: (code) => faults.push(code),
    });
    input.write(JSON.stringify(initMessage) + "\n");
    assert.equal(await h.done, 1);
    assert.deepEqual(faults, ["STDIO_IO_FAILED"]);
  },
);

test(
  "negotiated protocol version and HTTP session reach later requests",
  { timeout: 5000 },
  async () => {
    let notify;
    const notified = new Promise((resolve) => {
      notify = resolve;
    });
    const calls = [];
    const h = await harness(async (_url, init) => {
      calls.push(init);
      if (init.method === "GET") return new Response(null, { status: 405 });
      const message = JSON.parse(init.body);
      if (message.method === "initialize")
        return new Response(
          JSON.stringify({
            jsonrpc: "2.0",
            id: 1,
            result: {
              protocolVersion: "2025-03-26",
              capabilities: {},
              serverInfo: { name: "fixture", version: "1" },
            },
          }),
          {
            headers: {
              "content-type": "application/json",
              "mcp-session-id": "fixture-session",
            },
          },
        );
      notify(init);
      return new Response(null, { status: 202 });
    });
    try {
      h.send(initMessage);
      await h.waitFor((frame) => frame.id === 1);
      h.send({ jsonrpc: "2.0", method: "notifications/initialized" });
      const next = await notified;
      assert.equal(next.headers.get("mcp-session-id"), "fixture-session");
      assert.equal(next.headers.get("mcp-protocol-version"), "2025-03-26");
    } finally {
      await h.close();
    }
  },
);

test(
  "an invalid negotiated version is refused without becoming a request header",
  { timeout: 5000 },
  async () => {
    let calls = 0;
    const h = await harness(async () => {
      calls++;
      return response(1, {
        protocolVersion: "unsupported",
        capabilities: {},
        serverInfo: { name: "fixture", version: "1" },
      });
    });
    h.send(initMessage);
    assert.equal(await h.done, 1);
    assert.equal(calls, 1);
    assert.equal(h.frames.length, 0);
    assert.deepEqual(h.faults, ["PROTOCOL_VERSION_REFUSED"]);
  },
);

test("a queued send cannot begin after close has won", async () => {
  let calls = 0;
  const h = await harness(async () => {
    calls++;
    return initialized();
  });
  h.send(initMessage);
  await h.close();
  await tick();
  assert.equal(calls, 0);
  assert.equal(await h.done, 0);
});

test("an already aborted caller cannot invoke even a supplied fetch", async () => {
  let calls = 0;
  const caller = new AbortController();
  caller.abort();
  const fetcher = createPinnedFetch(
    config(),
    async () => {
      calls++;
      return response(1);
    },
    new AbortController().signal,
  );
  await assert.rejects(fetcher(endpoint, { signal: caller.signal }), {
    name: "AbortError",
  });
  assert.equal(calls, 0);
});

test("Request inputs preserve their body while removing unrelated credentials", async () => {
  let captured;
  const fetcher = createPinnedFetch(
    config(),
    async (request, init) => {
      captured = { body: await request.clone().text(), init };
      return response(1);
    },
    new AbortController().signal,
  );
  await fetcher(
    new Request(endpoint, {
      method: "POST",
      body: "fixture-body",
      headers: { Authorization: "unrelated" },
    }),
  );
  assert.equal(captured.body, "fixture-body");
  assert.equal(captured.init.headers.has("authorization"), false);
  assert.equal(captured.init.headers.get("x-api-key"), key);
});

test(
  "header deadline aborts stalled headers but releases its timer after headers",
  { timeout: 5000 },
  async () => {
    const stop = new AbortController();
    const stalled = createPinnedFetch(
      config(),
      async (_url, init) =>
        new Promise((_resolve, reject) => {
          init.signal.addEventListener(
            "abort",
            () => reject(init.signal.reason),
            { once: true },
          );
        }),
      stop.signal,
      { headerTimeoutMs: 10 },
    );
    await assert.rejects(stalled(endpoint), { name: "TimeoutError" });
    let observed;
    const ready = createPinnedFetch(
      config(),
      async (_url, init) => {
        observed = init.signal;
        return response(1);
      },
      stop.signal,
      { headerTimeoutMs: 10 },
    );
    const result = await ready(endpoint);
    await new Promise((resolve) => setTimeout(resolve, 30));
    assert.equal(
      observed.aborted,
      false,
      "headers deadline must not limit response body lifetime",
    );
    await result.text();
    stop.abort();
    assert.equal(observed.aborted, true);
  },
);

test(
  "open POST/SSE and GET bodies occupy the HTTP limit until consumed or cancelled",
  { timeout: 5000 },
  async () => {
    const stop = new AbortController();
    let calls = 0;
    let cancellations = 0;
    const fetcher = createPinnedFetch(
      config(),
      async () => {
        calls++;
        return new Response(
          new ReadableStream({
            cancel() {
              cancellations++;
            },
          }),
          {
            headers: { "content-type": "text/event-stream" },
          },
        );
      },
      stop.signal,
      { maxActiveResponses: 2 },
    );
    const first = await fetcher(endpoint, { method: "POST" });
    const second = await fetcher(endpoint, { method: "GET" });
    await assert.rejects(fetcher(endpoint), /BRIDGE_BUSY/);
    assert.equal(calls, 2);
    await first.body.cancel();
    const third = await fetcher(endpoint);
    assert.equal(calls, 3);
    stop.abort();
    await tick();
    assert.equal(cancellations, 3);
    await Promise.allSettled([second.body.cancel(), third.body.cancel()]);
  },
);

test(
  "fast SSE headers do not release the bridge request budget",
  { timeout: 5000 },
  async () => {
    let calls = 0;
    const h = await harness(
      async () => {
        calls++;
        return new Response(new ReadableStream({}), {
          headers: { "content-type": "text/event-stream" },
        });
      },
      { maxInFlight: 1 },
    );
    h.send({ jsonrpc: "2.0", id: 7, method: "ping" });
    await tick();
    await tick();
    h.send({ jsonrpc: "2.0", id: 8, method: "ping" });
    assert.equal(await h.done, 1);
    assert.equal(calls, 1);
    assert.deepEqual(h.faults, ["BRIDGE_BUSY"]);
  },
);

test("HTTP slots release once after EOF, body error, fetch error and refused response", async () => {
  const stop = new AbortController();
  let next;
  let refusedCancelled = 0;
  const fetcher = createPinnedFetch(config(), async () => next(), stop.signal, {
    maxActiveResponses: 1,
  });
  next = () => {
    throw new Error("fixture fetch error");
  };
  await assert.rejects(fetcher(endpoint), /fixture fetch error/);
  for (const status of [302, 401]) {
    next = () =>
      new Response(
        new ReadableStream({
          cancel() {
            refusedCancelled++;
          },
        }),
        { status },
      );
    await assert.rejects(
      fetcher(endpoint),
      status === 302 ? /REMOTE_REDIRECT_REFUSED/ : /REMOTE_AUTH_REQUIRED/,
    );
  }
  assert.equal(refusedCancelled, 2);
  next = () =>
    new Response(
      new ReadableStream({
        start(c) {
          c.error(new Error("fixture body error"));
        },
      }),
    );
  await assert.rejects((await fetcher(endpoint)).text(), /fixture body error/);
  next = () => response(1);
  await (await fetcher(endpoint)).text();
  const a = await fetcher(endpoint);
  await assert.rejects(fetcher(endpoint), /BRIDGE_BUSY/);
  await a.body.cancel();
  const b = await fetcher(endpoint);
  await assert.rejects(fetcher(endpoint), /BRIDGE_BUSY/);
  await b.text();
  stop.abort();
});

test("body wrapper preserves response metadata", async () => {
  const original = response(1);
  Object.defineProperty(original, "url", { value: endpoint });
  const fetcher = createPinnedFetch(
    config(),
    async () => original,
    new AbortController().signal,
  );
  const wrapped = await fetcher(endpoint);
  for (const property of ["url", "type", "redirected", "status", "statusText"])
    assert.equal(wrapped[property], original[property]);
  assert.deepEqual([...wrapped.headers], [...original.headers]);
  assert.equal((await wrapped.json()).id, 1);
});

test(
  "modern discovery declines locally before legacy initialize on the same connection",
  { timeout: 1500 },
  async () => {
    const calls = [];
    const h = await harness(async (_url, init) => {
      const frame = JSON.parse(init.body);
      calls.push(frame);
      if (frame.method === "initialize") return initialized();
      return response(frame.id, { tools: [] });
    });
    try {
      h.send({
        jsonrpc: "2.0",
        id: "desktop-probe",
        method: "server/discover",
        params: {},
      });
      await tick();
      await tick();
      assert.equal(
        calls.length,
        0,
        "probe must not reach a legacy HTTP backend",
      );
      const declined = await h.waitFor((frame) => frame.id === "desktop-probe");
      assert.equal(declined.error.code, -32601);
      h.send(initMessage);
      assert.equal(
        (await h.waitFor((frame) => frame.id === 1)).result.protocolVersion,
        "2025-11-25",
      );
      h.send({ jsonrpc: "2.0", id: 2, method: "tools/list", params: {} });
      assert.deepEqual(
        (await h.waitFor((frame) => frame.id === 2)).result.tools,
        [],
      );
      assert.equal(calls.length, 2);
      assert.deepEqual(h.faults, []);
    } finally {
      await h.close();
    }
  },
);
