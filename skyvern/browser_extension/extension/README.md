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

6. The pairing page hands off automatically; click **Approve pairing** in the **Skyvern Agent** confirmation tab
   (the single approval step).

The local pairing page checks that the extension is available before it claims the single-use offer. If pairing starts
before the extension is loaded, keep the page open. It retains the offer and continues automatically after the
extension becomes available.

7. Add controllable tabs to the **Skyvern Controlled** group.

Skyvern moves popups from controlled tabs into tabs in the opener's **Skyvern Controlled** group.
The new tab becomes active only if the opener was active.
If Chrome rejects the move, Skyvern keeps control of the separate popup.

Every controlled HTTP(S) tab shows a glowing frame and the label "Skyvern is controlling this tab" while the agent is connected.
To stop control of a tab, select **Remove from Skyvern Controlled** in the extension popup.
You can also drag the tab out of the group.
Tabs open before an extension update show the frame after their next reload or navigation.
A fullscreen video or element covers the frame.

Direct JavaScript evaluation is unavailable in extension mode. The `dom.evaluate` operation returns `OP_NOT_ALLOWED`:

> Direct JavaScript evaluation is unavailable in extension mode. Use skyvern_observe, skyvern_get_html, skyvern_find, or skyvern_get_value to inspect the page. Use skyvern_click or skyvern_type to interact.

This restriction also applies to `skyvern_evaluate` and tools that call it.
Use the inspection and interaction tools. Do not reroute the expression through CDP or another injection API.
Fixed value fills with `skyvern_type(input_method="value")` remain available. A debugger disconnect is a separate connection problem.

On POSIX, extension mode uses the persistent broker by default. The first broker start automatically validates or
initializes its journal and copies an existing legacy credential into the owner-only broker run directory, or creates
matching owner-only legacy and broker credentials. `skyvern browser extension-broker-enable` is still available as an
explicit idempotent enable/start command, but normal setup does not require it.

The broker keeps the extension-facing port reserved after an MCP process exits, and multiple MCP agents share one
daemon concurrently. A successful pairing creates one persisted workstation approval shared by all MCP agents. Revoke
it with `skyvern browser extension-revoke-workstation`; add `--all` to clear live interactive approvals as the full
kill switch. Interactive approval is bound to a continuously connected agent: it survives overlap socket replacement
(needed for MV3 and network reconnects) but dies on a true disconnect. Each approved agent leases its own tabs, popups
follow their opener, and user-shared tabs go to the first agent that claims them. A disconnected extension gets a
short reconnect window before session creation opens the one-click pairing page for you. Use
`skyvern browser extension-broker-status` to inspect sanitized state and `skyvern browser extension-broker-stop` to
drain the daemon and release the configured port.

The extension records broker-created root tabs and popups in Chrome session storage while it owns them. When a tab
leaves scope, including after an external debugger detach, the extension gives up that ownership and leaves the tab
open. Later reset cleanup cannot close a handed-back tab. Controlling it again requires the operator to share it again.
Explicit removal of an actively scoped tab and cleanup of incomplete tab creation still close those tabs.

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

Debugger-backed tools display Chrome's debugger infobar. Clicking **Cancel** in
the infobar revokes debugger access. Removing a tab with the popup or dragging it out of **Skyvern Controlled** revokes
all extension access to that tab.

For the complete setup, security model, and limitations, see
[Control Your Chrome with Skyvern Agent](../../../docs/developers/optimization/chrome-extension.mdx).
