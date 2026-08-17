# Install Skyvern Agent for development

The **Skyvern Agent** extension is plain JavaScript and does not require a build step. The recommended setup is:

```bash
skyvern browser extension-install
```

This prints the unpacked extension directory and best-effort opens `chrome://extensions`.

To set it up manually:

1. Print the unpacked extension directory:

   ```bash
   skyvern browser extension-path
   ```

2. Open `chrome://extensions`, enable **Developer mode**, and click **Load unpacked**.
3. Select the directory printed by the command.
4. Start the local MCP server in extension mode:

   ```bash
   skyvern run mcp --browser-extension
   ```

5. Start explicit pairing:

   ```bash
   skyvern browser extension-pair
   ```

6. Click **Approve** in the pairing page, then approve pairing in the **Skyvern Agent** confirmation tab.
7. Add controllable tabs to the **Skyvern Controlled** group.

On POSIX, extension mode uses the persistent broker by default. The first broker start automatically validates or
initializes its journal and copies an existing legacy credential into the owner-only broker run directory, or creates
matching owner-only legacy and broker credentials. `skyvern browser extension-broker-enable` is still available as an
explicit idempotent enable/start command, but normal setup does not require it.

The broker keeps the extension-facing port reserved after an MCP process exits. Pairing remains explicit, and a
disconnected extension receives up to 35 seconds to reconnect before session creation returns guidance. Use
`skyvern browser extension-broker-status` to inspect sanitized state and `skyvern browser extension-broker-stop` to
drain the daemon and release the configured port.

To opt into the legacy embedded relay on POSIX, set exactly:

```bash
SKYVERN_BROWSER_EXTENSION_BROKER=0
```

Unset, `1`, and every other value use the broker. The opt-out may be placed in the same environment or env-file chain
used by the Skyvern CLI. Windows currently uses the legacy path automatically and logs broker code
`UNSUPPORTED_PLATFORM`; no opt-out setting is required.

The extension connects outbound to `ws://127.0.0.1:19777/extension/v1` by default. To use another port, set
`SKYVERN_BROWSER_EXTENSION_PORT` for the MCP process and enter the same port under **Advanced settings** in the popup.

In broker mode, `extension.secret` is daemon-owned and `skyvern browser extension-token` intentionally refuses to copy
it. Use the explicit pairing command instead. The popup token-paste flow remains available only with the legacy opt-out.

Chrome's debugger infobar remains visible while Skyvern controls a tab. Clicking **Cancel**, removing the tab with the
popup, or dragging the tab out of **Skyvern Controlled** revokes access.

For the complete setup, security model, and limitations, see
[Control Your Chrome with Skyvern Agent](../../../docs/developers/optimization/chrome-extension.mdx).
