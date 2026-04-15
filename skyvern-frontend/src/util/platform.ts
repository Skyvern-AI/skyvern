// True when running in a Mac/iOS user agent. Prefers the newer
// `navigator.userAgentData.platform` (User-Agent Client Hints) when the
// browser exposes it, and falls back to `navigator.userAgent` string
// sniffing. `navigator.platform` is deliberately avoided because it's
// spec-deprecated and produces console warnings in recent Chrome.
//
// Safe to call in SSR contexts: returns `false` when `navigator` is
// undefined.
export function isMacPlatform(): boolean {
  if (typeof navigator === "undefined") return false;

  const uaData = (
    navigator as Navigator & {
      userAgentData?: { platform?: string };
    }
  ).userAgentData;
  if (uaData && typeof uaData.platform === "string") {
    return /mac/i.test(uaData.platform);
  }

  return /mac|iphone|ipad|ipod/i.test(navigator.userAgent || "");
}
