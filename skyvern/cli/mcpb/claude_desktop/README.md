# Skyvern Claude Desktop API-key fallback

This local candidate uses a small stdio-to-Streamable-HTTP entrypoint with the MCP client, core and stdio server packages pinned to 2.3.1. It supports API-key access to an HTTPS Skyvern endpoint using the legacy MCP initialize handshake. Modern server/discover probes receive a local Method not found response, allowing clients to fall back on the same stdio connection. Modern-only negotiation is not supported. It does not load an OAuth provider, discover an authorization server, read a token store or refresh tokens.

The custom connector with browser sign-in remains the recommended Skyvern Cloud route. This fallback is for API-key deployments and is shared with the Python CLI configuration.

## Prepare and check

```sh
npm ci --ignore-scripts
npm run check
npm test
```

Keep the generated lockfile. Runtime packaging needs only `npm ci --omit=dev --ignore-scripts` and the manifest, icon, desktop.mjs, desktop-ca.mjs, proxy.mjs and runtime node_modules. The checked-in `.mcpb` contains this local candidate. Maintainers own upstream review and publication.

## Configuration

The bundle descriptor supplies `SKYVERN_API_KEY` and `SKYVERN_MCP_URL` through environment entries. The API key is not a process argument. The endpoint must use HTTPS, without userinfo or a fragment. Configure the final URL: automatic redirects are refused, including same-origin redirects. Requests are restricted to the configured origin and carry only the configured API key plus the protocol headers; OAuth Authorization, Proxy-Authorization and Cookie headers are removed.

The equivalent manual Desktop configuration can launch the **same prepared entrypoint**:

```json
{
  "mcpServers": {
    "skyvern": {
      "command": "node",
      "args": ["/absolute/path/to/prepared-bundle/proxy.mjs"],
      "env": {
        "SKYVERN_API_KEY": "YOUR_SKYVERN_API_KEY",
        "SKYVERN_MCP_URL": "https://api.skyvern.com/mcp/"
      }
    }
  }
}
```

Replace the path with the directory containing proxy.mjs and its locked runtime node_modules. This is not an npm command or a claim that a new public package exists. The Python `skyvern setup claude` command extracts the bundled runtime and configures this same proxy without npx or a runtime package download. Manual CLI configuration uses system Node; for private HTTPS it forwards NODE_EXTRA_CA_CERTS at process startup.

A 401 stops with `REMOTE_AUTH_REQUIRED`; it does not open a browser or retry with stored credentials. Transport failures emit fixed diagnostic codes, not server error text or the API key. Each fetch has a 30-second response-header deadline, cleared when headers arrive; long-lived SSE bodies remain subject to caller and shutdown cancellation. At most 64 HTTP responses may be active, from fetch start through body EOF, error or cancellation, including background GET/SSE streams. The local stdio buffer is capped at 10 MiB and concurrent forwarding writes at 64. Reaching a limit stops the bridge with BRIDGE_BUSY. EOF and signals close the SDK transports. Closing a connection does not establish whether a remote in-flight tool action completed; this component does not automatically replay it.

Tests use real SDK transports with in-memory stdio and mocked fetch. Live Skyvern/Claude authentication, real network behavior and Desktop installation are separate acceptance steps. Do not describe this scoped candidate as remediation of every MCP/OAuth deployment.

Local HTTPS deployments can set the optional Additional trusted CA certificate path. The Desktop entrypoint is desktop.mjs; the shared proxy.mjs is also used by the Python CLI. The extension passes the CA path as SKYVERN_CA_CERTIFICATE and adds its PEM certificates to the existing thread-local Node TLS defaults before loading the bridge. This also works when Desktop filters NODE_EXTRA_CA_CERTS. It never disables certificate or hostname checks and does not change OS trust. The bundle requires Node 22.19.x or 24.5+ for this API. Desktop can supply a compatible built-in runtime; a separate system Node installation is needed for the manual CLI route. Local Desktop acceptance and its exact versions are recorded in the accompanying report; Skyvern Cloud was not exercised without a Cloud key.
