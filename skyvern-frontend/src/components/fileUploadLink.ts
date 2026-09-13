// A local-storage deployment answers an upload with the server's own file path, which no browser
// can open, so the name is shown without a link rather than as one that goes nowhere.
export function isBrowserFetchableUrl(url: string): boolean {
  return /^https?:\/\//i.test(url);
}
