export interface VncClipboardRfb {
  clipboardPasteFrom(text: string): void;
  sendKey(keysym: number, code: string, down?: boolean): void;
}

export type HeldMetaSides = { left: boolean; right: boolean };

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
const VNC_C_KEYSYM = 0x0063;
const VNC_V_KEYSYM = 0x0076;
// noVNC maps macOS right Cmd to Super_L; BrowserStream sends left Cmd as Super_L too instead of noVNC's Alt_L.
const VNC_SUPER_L_KEYSYM = 0xffeb;
const VNC_CMD_LEFT_RELEASE_KEYSYM = VNC_SUPER_L_KEYSYM;
const VNC_CMD_RIGHT_RELEASE_KEYSYM = VNC_SUPER_L_KEYSYM;
const VNC_BLANKET_META_RELEASES: Array<{ keysym: number; code: string }> = [
  { keysym: 0xffe9, code: "AltLeft" },
  { keysym: 0xffea, code: "AltRight" },
  { keysym: 0xffeb, code: "MetaLeft" },
  { keysym: 0xffec, code: "MetaRight" },
];
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

function isClipboardCopyShortcut(event: PasteShortcutEvent) {
  // Ctrl+C already reaches the remote browser as Ctrl+C; only Cmd+C needs rewriting.
  return (
    event.metaKey &&
    !event.ctrlKey &&
    !event.altKey &&
    event.key.toLowerCase() === "c"
  );
}

function sendVncControlShortcut(
  rfb: VncClipboardRfb,
  keysym: number,
  code: string,
  releaseMetaModifiers = false,
) {
  if (releaseMetaModifiers) {
    for (const { keysym, code } of VNC_BLANKET_META_RELEASES) {
      rfb.sendKey(keysym, code, false);
    }
  }
  rfb.sendKey(VNC_CONTROL_LEFT_KEYSYM, "ControlLeft", true);
  rfb.sendKey(keysym, code, true);
  rfb.sendKey(keysym, code, false);
  rfb.sendKey(VNC_CONTROL_LEFT_KEYSYM, "ControlLeft", false);
}

function sendVncPasteShortcut(
  rfb: VncClipboardRfb,
  releaseMetaModifiers = false,
) {
  sendVncControlShortcut(rfb, VNC_V_KEYSYM, "KeyV", releaseMetaModifiers);
}

// The remote browser runs on Linux, where Cmd means nothing: send the Ctrl
// shortcut with Cmd lifted, then put back whichever Cmd keys are still held.
function sendVncControlShortcutForChord(
  event: Pick<KeyboardEvent, "metaKey">,
  rfb: VncClipboardRfb,
  keysym: number,
  code: string,
  getHeldMetaSides?: () => HeldMetaSides,
) {
  if (!event.metaKey) {
    sendVncControlShortcut(rfb, keysym, code);
    return;
  }

  const sides = getHeldMetaSides?.();
  if (!sides?.left && !sides?.right) {
    sendVncControlShortcut(rfb, keysym, code, true);
    return;
  }

  if (sides.left) {
    rfb.sendKey(VNC_CMD_LEFT_RELEASE_KEYSYM, "MetaLeft", false);
  }
  if (sides.right) {
    rfb.sendKey(VNC_CMD_RIGHT_RELEASE_KEYSYM, "MetaRight", false);
  }

  sendVncControlShortcut(rfb, keysym, code);

  const currentMetaSides = getHeldMetaSides?.();
  if (currentMetaSides?.left) {
    rfb.sendKey(VNC_CMD_LEFT_RELEASE_KEYSYM, "MetaLeft", true);
  }
  if (currentMetaSides?.right) {
    rfb.sendKey(VNC_CMD_RIGHT_RELEASE_KEYSYM, "MetaRight", true);
  }
}

// The remote browser copies into its own clipboard, which the VNC server then
// sends back; the stream turns that into a local clipboard write.
function handleVncClipboardCopyShortcut(
  event: PasteShortcutEvent,
  rfb: VncClipboardRfb | null,
  getHeldMetaSides?: () => HeldMetaSides,
) {
  if (!rfb || !isClipboardCopyShortcut(event)) {
    return false;
  }
  event.preventDefault();
  event.stopPropagation();
  event.stopImmediatePropagation();
  sendVncControlShortcutForChord(
    event,
    rfb,
    VNC_C_KEYSYM,
    "KeyC",
    getHeldMetaSides,
  );
  return true;
}

// x11vnc sends the remote clipboard as UTF-8 bytes, which noVNC hands over one
// character per byte. Text that is not valid UTF-8 is kept as Latin-1.
function decodeVncClipboardText(text: string) {
  const codes = Array.from(text, (char) => char.charCodeAt(0));
  if (codes.some((code) => code > 0xff)) {
    return text;
  }
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(
      Uint8Array.from(codes),
    );
  } catch {
    return text;
  }
}

async function syncTextToVnc(
  rfb: VncClipboardRfb,
  text: string,
  syncDelayMs = DEFAULT_VNC_CLIPBOARD_SYNC_DELAY_MS,
) {
  rfb.clipboardPasteFrom(text);
  if (syncDelayMs > 0) {
    await sleep(syncDelayMs);
  }
}

// For a paste started without a keystroke (a button), so no modifiers are held.
async function pasteTextIntoVnc(rfb: VncClipboardRfb, text: string) {
  await syncTextToVnc(rfb, text);
  sendVncPasteShortcut(rfb);
}

async function handleVncClipboardPasteShortcut(
  event: PasteShortcutEvent,
  rfb: VncClipboardRfb | null,
  options?: {
    readClipboardText?: () => Promise<string>;
    syncDelayMs?: number;
    onPasteError?: (err: unknown) => void;
    onPasted?: (text: string) => void;
    onEmptyClipboard?: () => void;
    getHeldMetaSides?: () => HeldMetaSides;
  },
) {
  if (!rfb || !isClipboardPasteShortcut(event)) {
    return false;
  }

  const {
    readClipboardText = () => navigator.clipboard.readText(),
    syncDelayMs,
    onPasteError,
    onPasted,
    onEmptyClipboard,
    getHeldMetaSides,
  } = options ?? {};

  event.preventDefault();
  event.stopPropagation();
  event.stopImmediatePropagation();

  let text: string;
  try {
    text = await readClipboardText();
    if (!text) {
      onEmptyClipboard?.();
      return true;
    }
    await syncTextToVnc(rfb, text, syncDelayMs);
  } catch (err) {
    console.error("Failed to sync clipboard contents to VNC:", err);
    onPasteError?.(err);
    return true;
  }
  onPasted?.(text);
  sendVncControlShortcutForChord(
    event,
    rfb,
    VNC_V_KEYSYM,
    "KeyV",
    getHeldMetaSides,
  );
  return true;
}

export {
  VNC_SUPER_L_KEYSYM,
  decodeVncClipboardText,
  handleVncClipboardCopyShortcut,
  handleVncClipboardPasteShortcut,
  isClipboardPasteShortcut,
  pasteTextIntoVnc,
  sendVncPasteShortcut,
};
