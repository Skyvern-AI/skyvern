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
const VNC_V_KEYSYM = 0x0076;
// noVNC maps physical macOS left Cmd to Alt_L and right Cmd to Super_L on this path.
const VNC_CMD_LEFT_RELEASE_KEYSYM = 0xffe9;
const VNC_CMD_RIGHT_RELEASE_KEYSYM = 0xffeb;
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

function sendVncPasteShortcut(
  rfb: VncClipboardRfb,
  releaseMetaModifiers = false,
) {
  if (releaseMetaModifiers) {
    for (const { keysym, code } of VNC_BLANKET_META_RELEASES) {
      rfb.sendKey(keysym, code, false);
    }
  }
  rfb.sendKey(VNC_CONTROL_LEFT_KEYSYM, "ControlLeft", true);
  rfb.sendKey(VNC_V_KEYSYM, "KeyV", true);
  rfb.sendKey(VNC_V_KEYSYM, "KeyV", false);
  rfb.sendKey(VNC_CONTROL_LEFT_KEYSYM, "ControlLeft", false);
}

async function handleVncClipboardPasteShortcut(
  event: PasteShortcutEvent,
  rfb: VncClipboardRfb | null,
  options?: {
    readClipboardText?: () => Promise<string>;
    syncDelayMs?: number;
    onPasteError?: (err: unknown) => void;
    getHeldMetaSides?: () => HeldMetaSides;
  },
) {
  if (!rfb || !isClipboardPasteShortcut(event)) {
    return false;
  }

  const {
    readClipboardText = () => navigator.clipboard.readText(),
    syncDelayMs = DEFAULT_VNC_CLIPBOARD_SYNC_DELAY_MS,
    onPasteError,
    getHeldMetaSides,
  } = options ?? {};

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
    onPasteError?.(err);
    return true;
  }

  if (!event.metaKey) {
    sendVncPasteShortcut(rfb);
    return true;
  }

  const sides = getHeldMetaSides?.();
  if (!sides?.left && !sides?.right) {
    sendVncPasteShortcut(rfb, true);
    return true;
  }

  if (sides.left) {
    rfb.sendKey(VNC_CMD_LEFT_RELEASE_KEYSYM, "MetaLeft", false);
  }
  if (sides.right) {
    rfb.sendKey(VNC_CMD_RIGHT_RELEASE_KEYSYM, "MetaRight", false);
  }

  sendVncPasteShortcut(rfb);

  const currentMetaSides = getHeldMetaSides?.();
  if (currentMetaSides?.left) {
    rfb.sendKey(VNC_CMD_LEFT_RELEASE_KEYSYM, "MetaLeft", true);
  }
  if (currentMetaSides?.right) {
    rfb.sendKey(VNC_CMD_RIGHT_RELEASE_KEYSYM, "MetaRight", true);
  }
  return true;
}

export {
  handleVncClipboardPasteShortcut,
  isClipboardPasteShortcut,
  sendVncPasteShortcut,
};
