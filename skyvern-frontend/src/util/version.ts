function formatVersion(version: string): string {
  // Truncate full git SHAs (40 hex chars) to short form
  if (/^[0-9a-f]{40}$/i.test(version)) {
    return version.slice(0, 7);
  }
  return version;
}

export { formatVersion };
