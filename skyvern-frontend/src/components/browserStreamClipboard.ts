export interface VncClipboardRfb {
  clipboardPasteFrom(text: string): void;
  sendKey(keysym: number, code: string, down?: boolean): void;
}

export type PasteShortcutEvent = Pick<
  KeyboardEvent,
  | "altKey"
  | "ctrlKey"
  | "key"
  | "metaKey"
  | "preventDefault"
  | "shiftKey"
  | "stopImmediatePropagation"
  | "stopPropagation"
>;

const VNC_CONTROL_LEFT_KEYSYM = 0xffe3;
const VNC_V_KEYSYM = 0x0076;
const DEFAULT_VNC_CLIPBOARD_SYNC_DELAY_MS = 50;

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function isClipboardPasteShortcut(event: PasteShortcutEvent) {
  // Ctrl+Shift+V is paste-without-formatting in many browser inputs. We only
  // exclude Alt-based shortcuts to match the backend RFB paste detection.
  return (
    (event.ctrlKey || event.metaKey) &&
    !event.altKey &&
    event.key.toLowerCase() === "v"
  );
}

function sendVncPasteShortcut(rfb: VncClipboardRfb) {
  rfb.sendKey(VNC_CONTROL_LEFT_KEYSYM, "ControlLeft", true);
  rfb.sendKey(VNC_V_KEYSYM, "KeyV", true);
  rfb.sendKey(VNC_V_KEYSYM, "KeyV", false);
  rfb.sendKey(VNC_CONTROL_LEFT_KEYSYM, "ControlLeft", false);
}

async function handleVncClipboardPasteShortcut(
  event: PasteShortcutEvent,
  rfb: VncClipboardRfb | null,
  readClipboardText = () => navigator.clipboard.readText(),
  syncDelayMs = DEFAULT_VNC_CLIPBOARD_SYNC_DELAY_MS,
) {
  if (!rfb || !isClipboardPasteShortcut(event)) {
    return false;
  }

  event.preventDefault();
  event.stopPropagation();
  event.stopImmediatePropagation();

  try {
    const text = await readClipboardText();
    rfb.clipboardPasteFrom(text);
    if (syncDelayMs > 0) {
      await sleep(syncDelayMs);
    }
  } catch (err) {
    console.error("Failed to sync clipboard contents to VNC:", err);
    return true;
  }

  sendVncPasteShortcut(rfb);
  return true;
}

export {
  handleVncClipboardPasteShortcut,
  isClipboardPasteShortcut,
  sendVncPasteShortcut,
};
