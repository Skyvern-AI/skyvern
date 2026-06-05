export interface TagResponse {
  value: string;
  source: string;
  set_at: string;
  set_by: string;
}

export interface TagsResponse {
  workflow_permanent_id: string;
  tags: Record<string, TagResponse>;
}

export interface TagKey {
  key: string;
  description: string | null;
  // Number of workflows currently carrying this key (from GET /tag-keys).
  workflow_count: number;
}

// Batch endpoint returns a flat key->value map per workflow (no per-tag
// metadata). Descriptions come from the tag-key registry (GET /tag-keys).
export interface WorkflowTagsBatchResponse {
  workflow_tags: Record<string, Record<string, string>>;
}

// Body for POST /workflows/{wpid}/tags. `tags` is set (overwrite, set-wins);
// `tags_to_delete` soft-deletes keys. Both optional; both empty is a no-op.
export interface TagApplyRequest {
  tags?: Record<string, string>;
  tags_to_delete?: Array<string>;
}

// Mirror skyvern/forge/sdk/workflow/models/validators.py so the editor rejects
// the same inputs the backend would 422 on.
export const TAG_KEY_REGEX = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;
export const RESERVED_TAG_KEY_PREFIX = "skyvern.";
export const MAX_TAGS_PER_WORKFLOW = 20;
export const TAG_KEY_MAX_LENGTH = 64;
export const TAG_VALUE_MAX_LENGTH = 256;

export function validateTagKey(key: string): string | null {
  const trimmed = key.trim();
  if (!trimmed) {
    return "Key is required.";
  }
  if (trimmed.length > TAG_KEY_MAX_LENGTH) {
    return `Key must be at most ${TAG_KEY_MAX_LENGTH} characters.`;
  }
  if (trimmed.startsWith(RESERVED_TAG_KEY_PREFIX)) {
    return `Keys can't start with the reserved "${RESERVED_TAG_KEY_PREFIX}" prefix.`;
  }
  if (!TAG_KEY_REGEX.test(trimmed)) {
    return "Use letters, digits, _ . or - (must start with a letter or digit).";
  }
  return null;
}

export function validateTagValue(value: string): string | null {
  const trimmed = value.trim();
  if (!trimmed) {
    return "Value is required.";
  }
  if (trimmed.length > TAG_VALUE_MAX_LENGTH) {
    return `Value must be at most ${TAG_VALUE_MAX_LENGTH} characters.`;
  }
  if (trimmed.includes(",")) {
    return "Value can't contain a comma.";
  }
  return null;
}

export interface TagFilterPair {
  key: string;
  value: string;
}

// Parse the `?tags=key:value,key:value` URL param into pairs. Forgiving on
// read (drops malformed/blank segments) so a hand-edited URL can't crash the
// page; the backend GET /workflows filter is strict, so we only ever serialize
// valid pairs back. Mirrors the backend's first-colon partition so values may
// themselves contain colons. Duplicate pairs are collapsed.
export function parseTagFilter(
  raw: string | null | undefined,
): TagFilterPair[] {
  if (!raw) {
    return [];
  }
  const pairs: TagFilterPair[] = [];
  const seen = new Set<string>();
  for (const segment of raw.split(",")) {
    const separatorIndex = segment.indexOf(":");
    if (separatorIndex <= 0) {
      continue;
    }
    const key = segment.slice(0, separatorIndex).trim();
    const value = segment.slice(separatorIndex + 1).trim();
    if (!key || !value) {
      continue;
    }
    const dedupeKey = `${key}:${value}`;
    if (seen.has(dedupeKey)) {
      continue;
    }
    seen.add(dedupeKey);
    pairs.push({ key, value });
  }
  return pairs;
}

// Canonical serialization: pairs are sorted by key then value so that
// semantically identical filters (the backend ANDs keys / ORs values, order
// is irrelevant) produce one stable string. This keeps the URL and the React
// Query key order-independent, so reordering can't cause a needless refetch.
export function serializeTagFilter(pairs: TagFilterPair[]): string {
  return [...pairs]
    .sort(
      (a, b) => a.key.localeCompare(b.key) || a.value.localeCompare(b.value),
    )
    .map((pair) => `${pair.key}:${pair.value}`)
    .join(",");
}
