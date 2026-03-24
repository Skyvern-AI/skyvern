function formatVersion(version: string): string {
  // Truncate full git SHAs (40 hex chars) to short form
  if (/^[0-9a-f]{40}$/i.test(version)) {
    return version.slice(0, 7);
  }
  return version;
}

/**
 * Safely access the build-time __APP_VERSION__ global.
 *
 * Vite's `define` does literal text replacement at build time. If the build
 * was produced without the `define` entry (e.g. self-hosted Docker images
 * built before the config was updated), the bare global reference throws a
 * ReferenceError at runtime. `typeof` on an undeclared variable returns
 * "undefined" without throwing, so this accessor is always safe.
 */
function getAppVersion(): string {
  return typeof __APP_VERSION__ !== "undefined"
    ? __APP_VERSION__
    : "development";
}

export { formatVersion, getAppVersion };
