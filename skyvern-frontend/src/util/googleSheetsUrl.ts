// Matches both /spreadsheets/d/<id> and the multi-account
// /spreadsheets/u/<n>/d/<id> shape Google emits when signed into multiple
// accounts.
const SPREADSHEET_URL_RE = /\/spreadsheets(?:\/u\/\d+)?\/d\/([a-zA-Z0-9-_]+)/;
// Published links use the form /spreadsheets/d/e/<token>/pub..., where the
// captured segment is "e" - a meaningless sentinel, not a spreadsheet id.
// The Sheets API can't read those URLs, so reject them outright.
const PUBLISHED_URL_RE = /\/spreadsheets\/d\/e\//;
const BARE_ID_RE = /^[a-zA-Z0-9-_]{20,}$/;

export function extractSpreadsheetIdFromUrl(input: string): string | null {
  if (!input) return null;
  if (isTemplateExpression(input)) return null;
  if (PUBLISHED_URL_RE.test(input)) return null;
  const match = input.match(SPREADSHEET_URL_RE);
  if (match) return match[1] ?? null;
  const trimmed = input.trim();
  if (BARE_ID_RE.test(trimmed)) return trimmed;
  return null;
}

export function buildSpreadsheetUrl(spreadsheetId: string): string {
  return `https://docs.google.com/spreadsheets/d/${spreadsheetId}/edit`;
}

export function isTemplateExpression(input: string): boolean {
  return input.includes("{{") || input.includes("{%");
}

// "A" -> 1, "Z" -> 26, "AA" -> 27. Returns 0 for anything that is not a real
// Sheets column reference: Google's per-tab cap is ZZZ (3 chars), so longer
// all-caps tokens like "TOTAL" must be treated as literals - otherwise an
// unmatched header name triggers a false-positive overflow warning.
export function columnLettersToIndex(letters: string): number {
  const upper = letters.toUpperCase();
  if (!/^[A-Z]{1,3}$/.test(upper)) return 0;
  let index = 0;
  for (const ch of upper) {
    index = index * 26 + (ch.charCodeAt(0) - "A".charCodeAt(0) + 1);
  }
  return index;
}
