const HTTP_URL_PATTERN = /\bhttps?:\/\/[^\s<>"']+/gi;

function safeHttpUrl(value: string): string | null {
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:"
      ? url.toString()
      : null;
  } catch {
    return null;
  }
}

function containsHttpUrl(value: string): boolean {
  for (const match of value.matchAll(HTTP_URL_PATTERN)) {
    if (safeHttpUrl(match[0]) !== null) {
      return true;
    }
  }

  return false;
}

export { containsHttpUrl, safeHttpUrl };
