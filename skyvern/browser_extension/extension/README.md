# Install Skyvern Agent for development

The **Skyvern Agent** extension is plain JavaScript and does not require a build step. The recommended setup is:

```bash
skyvern browser extension-install
```

This prints the unpacked extension directory, best-effort opens `chrome://extensions`, and prints the remaining
numbered steps. If the MCP extension bridge is already running, it also starts one-click pairing.

To set it up manually:

1. Print the unpacked extension directory:

   ```bash
   skyvern browser extension-path
   ```

2. Open `chrome://extensions`, enable **Developer mode**, and click **Load unpacked**.
3. Select the directory printed by the command.
4. Start the local MCP server with the extension bridge enabled:

   ```bash
   skyvern run mcp --browser-extension
   ```

5. Start the recommended one-click pairing flow:

   ```bash
   skyvern browser extension-pair
   ```

6. Click **Approve** in the pairing page, then approve the pairing in the **Skyvern Agent** confirmation tab that opens.
   The link expires after two minutes and is single-use, so rerun the command when needed.
7. Add any tab you want Skyvern to control to the **Skyvern Controlled** tab group. Dragging a tab into the group grants
   access and dragging it out revokes access. The popup's **Add to Skyvern Controlled** and
   **Remove from Skyvern Controlled** buttons perform the same actions.

If one-click pairing is unavailable, run `skyvern browser extension-token`, open the **Skyvern Agent** popup, paste the
token, and click **Connect**. This popup-paste path is the manual fallback.

The local Skyvern MCP process runs the bridge on `ws://127.0.0.1:19777/extension/v1` by default, and the extension
connects outbound to it. To use another port, set `SKYVERN_BROWSER_EXTENSION_PORT` for the MCP process and enter the
same port under **Advanced settings** in the popup.

Check the token configuration, token-file permissions, and bridge status without displaying the token:

```bash
skyvern browser extension-status
```

Chrome's debugger infobar remains visible while Skyvern controls a tab. Clicking **Cancel**, removing the tab with the
popup, or dragging the tab out of **Skyvern Controlled** revokes access.

For the complete setup, security model, and limitations, see
[Control Your Chrome with Skyvern Agent](../../../docs/developers/optimization/chrome-extension.mdx).
