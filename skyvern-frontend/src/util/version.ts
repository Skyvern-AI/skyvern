// This file exports version information for the Skyvern frontend
// The values are set at build time via environment variables

const gitSha = import.meta.env.VITE_GIT_SHA || "unknown";

export const FRONTEND_VERSION = {
  // Git commit SHA - injected at build time (first 7 characters only)
  gitSha: gitSha !== "unknown" ? gitSha.substring(0, 7) : "unknown",
  // Build timestamp - injected at build time
  buildTime: import.meta.env.VITE_BUILD_TIME || "unknown",
  // Version from skyvern-ts/client/src/version.ts (injected at build time)
  version: import.meta.env.VITE_APP_VERSION || "unknown",
  // Helper to check if version info is available
  isAvailable: gitSha !== "unknown",
};
