import { afterEach, describe, expect, it, vi } from "vitest";

import { copyText } from "./copyText";

afterEach(() => {
  vi.unstubAllGlobals();
  document.body.replaceChildren();
});

describe("HTTP clipboard fallback", () => {
  it.each([true, false, "throw"])(
    "restores stream focus and removes the textarea when copying returns %s",
    async (outcome) => {
      vi.stubGlobal("isSecureContext", false);
      const stream = document.createElement("div");
      stream.tabIndex = 0;
      document.body.appendChild(stream);
      stream.focus();
      const onKeyDown = vi.fn();
      stream.addEventListener("keydown", onKeyDown);
      const execCommand = vi.fn(() => {
        expect(document.activeElement).toBeInstanceOf(HTMLTextAreaElement);
        if (outcome === "throw") throw new Error("copy failed");
        return outcome;
      });
      Object.defineProperty(document, "execCommand", {
        configurable: true,
        value: execCommand,
      });

      try {
        if (outcome === "throw") {
          await expect(copyText("remote selection")).rejects.toThrow(
            "copy failed",
          );
        } else {
          await expect(copyText("remote selection")).resolves.toBe(outcome);
        }
        expect(execCommand).toHaveBeenCalledWith("copy");
        expect(document.querySelector("textarea")).toBeNull();
        expect(document.activeElement).toBe(stream);
        document.activeElement?.dispatchEvent(
          new KeyboardEvent("keydown", { key: "a" }),
        );
        expect(onKeyDown).toHaveBeenCalledOnce();
      } finally {
        Reflect.deleteProperty(document, "execCommand");
      }
    },
  );
});
