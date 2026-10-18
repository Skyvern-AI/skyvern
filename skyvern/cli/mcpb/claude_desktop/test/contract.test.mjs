import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
const root = new URL("../", import.meta.url);
const manifest = JSON.parse(
  readFileSync(new URL("manifest.json", root), "utf8"),
);
const pkg = JSON.parse(readFileSync(new URL("package.json", root), "utf8"));
test("bundle uses its own API-key-only entrypoint", () => {
  assert.equal(manifest.server.entry_point, "desktop.mjs");
  assert.equal(manifest.server.mcp_config.args[0], "${__dirname}/desktop.mjs");
  assert.ok(existsSync(new URL("proxy.mjs", root)));
});
test("credential is supplied through environment, not process arguments", () => {
  assert.equal(
    manifest.server.mcp_config.env.SKYVERN_API_KEY,
    "${user_config.api_key}",
  );
  assert.equal(
    manifest.server.mcp_config.env.SKYVERN_MCP_URL,
    "${user_config.server_url}",
  );
  assert.deepEqual(manifest.server.mcp_config.args, [
    "${__dirname}/desktop.mjs",
  ]);
  assert.equal(manifest.user_config.api_key.sensitive, true);
});
test("client, core and stdio server are pinned together; general bridge removed", () => {
  assert.deepEqual(pkg.dependencies, {
    "@modelcontextprotocol/client": "2.3.1",
    "@modelcontextprotocol/core": "2.3.1",
    "@modelcontextprotocol/server": "2.3.1",
  });
  assert.equal(pkg.dependencies["mcp-remote"], undefined);
});

test("private CA uses a dedicated environment key in embedded Node", () => {
  assert.equal(
    manifest.server.mcp_config.env.SKYVERN_CA_CERTIFICATE,
    "${user_config.ca_certificate}",
  );
  assert.equal(manifest.server.mcp_config.env.NODE_EXTRA_CA_CERTS, undefined);
});
