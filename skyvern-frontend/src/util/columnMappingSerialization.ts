// _id is a runtime-only stable React key; it is never serialized.
export type ColumnMappingEntry = {
  key: string;
  letter: string;
  _id?: string;
};

export function newEntryId(): string {
  if (
    typeof crypto !== "undefined" &&
    typeof crypto.randomUUID === "function"
  ) {
    return crypto.randomUUID();
  }
  return `cm-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

// Google Sheets columns top out at XFD (3 chars), so reject longer all-caps
// strings - otherwise an unmatched header like "TOTAL" gets treated as a
// column reference instead of preserved as a literal.
const LETTER_RE = /^[A-Z]{1,3}$/;

export function parseColumnMapping(json: string): ColumnMappingEntry[] {
  const trimmed = json.trim();
  if (!trimmed) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    return [];
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    return [];
  }
  return Object.entries(parsed as Record<string, unknown>).map(
    ([key, value]) => ({ key, letter: String(value) }),
  );
}

export function serializeColumnMapping(entries: ColumnMappingEntry[]): string {
  const deduped: Record<string, string> = {};
  for (const { key, letter } of entries) {
    if (!key || !letter) continue;
    deduped[key] = letter;
  }
  if (Object.keys(deduped).length === 0) return "";
  return JSON.stringify(deduped);
}

export function resolveDestination(
  input: string,
  headers: Array<{ letter: string; name: string }>,
): string {
  const trimmed = input.trim();
  if (!trimmed) return "";
  const match = headers.find(
    (h) => h.name.toLowerCase() === trimmed.toLowerCase(),
  );
  if (match) return match.letter;
  const upper = trimmed.toUpperCase();
  if (LETTER_RE.test(upper)) return upper;
  return trimmed;
}
