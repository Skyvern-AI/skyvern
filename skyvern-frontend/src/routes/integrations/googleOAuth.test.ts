import { describe, expect, it, vi } from "vitest";

import { storeGoogleOAuthIntegrationIdForState } from "./googleOAuth";

describe("Google OAuth state storage", () => {
  it("stores the integration id in an explicitly supplied window", () => {
    const setItem = vi.fn();
    const storageWindow = {
      sessionStorage: { setItem },
    } as unknown as Pick<Window, "sessionStorage">;

    storeGoogleOAuthIntegrationIdForState(
      "state_1",
      "google_sheets",
      storageWindow,
    );

    expect(setItem).toHaveBeenCalledWith(
      "skyvern:google-oauth-integration:state_1",
      "google_sheets",
    );
  });
});
