const GOOGLE_OAUTH_POPUP_STORAGE_KEY = "skyvern:google-oauth-popup";

type GoogleOAuthPopupWindow = Pick<Window, "close"> & {
  sessionStorage: Pick<Storage, "getItem" | "setItem" | "removeItem">;
};

export function markGoogleOAuthPopup(
  popup: Pick<GoogleOAuthPopupWindow, "sessionStorage">,
): void {
  try {
    popup.sessionStorage.setItem(GOOGLE_OAUTH_POPUP_STORAGE_KEY, "1");
  } catch {
    // Storage is best effort. OAuth can still finish in the integrations page.
  }
}

export function closeGoogleOAuthPopupIfMarked(
  popup: GoogleOAuthPopupWindow = window,
): boolean {
  try {
    if (popup.sessionStorage.getItem(GOOGLE_OAUTH_POPUP_STORAGE_KEY) !== "1") {
      return false;
    }
    popup.sessionStorage.removeItem(GOOGLE_OAUTH_POPUP_STORAGE_KEY);
    popup.close();
    return true;
  } catch {
    return false;
  }
}
