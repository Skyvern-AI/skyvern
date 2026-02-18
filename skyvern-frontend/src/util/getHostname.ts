/**
 * Extract the hostname from a URL string.
 * Returns the hostname on success, or null if the URL is invalid.
 */
export function getHostname(url: string): string | null {
  try {
    return new URL(url).hostname;
  } catch {
    return null;
  }
}
