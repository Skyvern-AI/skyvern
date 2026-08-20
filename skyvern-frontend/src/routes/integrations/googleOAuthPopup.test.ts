import { describe, expect, it, vi } from "vitest";

import {
  closeGoogleOAuthPopupIfMarked,
  markGoogleOAuthPopup,
} from "./googleOAuthPopup";

describe("Google OAuth popup state", () => {
  it("marks the popup and closes it after a successful callback", () => {
    const values = new Map<string, string>();
    const popup = {
      sessionStorage: {
        getItem: (key: string) => values.get(key) ?? null,
        setItem: (key: string, value: string) => values.set(key, value),
        removeItem: (key: string) => values.delete(key),
      },
      close: vi.fn(),
    };

    markGoogleOAuthPopup(popup);
    expect(closeGoogleOAuthPopupIfMarked(popup)).toBe(true);
    expect(popup.close).toHaveBeenCalledTimes(1);
    expect(values.size).toBe(0);
  });

  it("leaves a normal integrations navigation open", () => {
    const popup = {
      sessionStorage: {
        getItem: () => null,
        setItem: vi.fn(),
        removeItem: vi.fn(),
      },
      close: vi.fn(),
    };

    expect(closeGoogleOAuthPopupIfMarked(popup)).toBe(false);
    expect(popup.close).not.toHaveBeenCalled();
  });
});
