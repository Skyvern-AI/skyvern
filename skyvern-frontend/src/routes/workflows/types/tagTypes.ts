// A tag is a `value` (always present) with an optional `key` (the group).
// `key === null` is a standalone label; a non-null key is a grouped label.
export interface Tag {
  key: string | null;
  value: string;
}

export interface TagResponse {
  key: string | null;
  value: string;
  source: string;
  set_at: string;
  set_by: string;
}

// Responses are lists (not key-maps) so standalone labels, which have no key,
// are representable.
export interface TagsResponse {
  workflow_permanent_id: string;
  tags: Array<TagResponse>;
}

export interface RunTagsResponse {
  workflow_run_id: string;
  tags: Array<TagResponse>;
}

export interface TagKey {
  key: string;
  description: string | null;
  // Number of workflows currently carrying this key (from GET /tag-keys).
  // Standalone labels are not registered here — the registry is groups only.
  workflow_count: number;
}

// Palette color assigned to a grouped (key, value) pair (from GET /tag-values).
// Standalone labels have no key and are not colored, so never appear here.
// `workflow_count` (# workflows currently carrying this (key, value)) drives the
// label-management usage column and delete blast-radius warning; it's optional so
// color-only consumers and pre-field stubs stay valid.
export interface TagValue {
  key: string;
  value: string;
  color: string;
  workflow_count?: number;
}

// Batch endpoint returns a list of {key, value} per workflow (no per-tag
// metadata). Descriptions come from the tag-key registry (GET /tag-keys).
export interface WorkflowTagsBatchResponse {
  workflow_tags: Record<string, Array<Tag>>;
}

export interface RunTagsBatchResponse {
  run_tags: Record<string, Array<Tag>>;
}

// Body for POST /workflows/{wpid}/tags. `tags` sets/overwrites ({key?, value});
// `tags_to_delete` removes a grouped tag by {key} or a label by {value}.
export interface TagInput {
  key?: string | null;
  value: string;
}

export type TagDeleteInput =
  | { key: string; value?: never }
  | { value: string; key?: never };

export interface TagApplyRequest {
  tags?: Array<TagInput>;
  tags_to_delete?: Array<TagDeleteInput>;
  // Map of grouped tag key -> palette color name for the value being set. Keys
  // absent here keep their existing color or get a random palette color server-side.
  colors?: Record<string, string>;
}

// Mirror skyvern/forge/sdk/workflow/models/validators.py so the editor rejects
// the same inputs the backend would 422 on.
export const TAG_KEY_REGEX = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;
export const RESERVED_TAG_KEY_PREFIX = "skyvern.";
export const MAX_TAGS_PER_WORKFLOW = 20;
export const TAG_KEY_MAX_LENGTH = 64;
export const TAG_VALUE_MAX_LENGTH = 256;
export const MAX_AUTOCOMPLETE_SUGGESTIONS = 6;

export function isSystemTagKey(key: string | null | undefined): boolean {
  return typeof key === "string" && key.startsWith(RESERVED_TAG_KEY_PREFIX);
}

export function isUserWritableTagKey(key: string | null | undefined): boolean {
  return !isSystemTagKey(key);
}

// Returns an error string when `key` (a group) is invalid, or null when valid.
export function validateTagKey(key: string): string | null {
  const trimmed = key.trim();
  if (!trimmed) {
    return "Group is required.";
  }
  if (trimmed.length > TAG_KEY_MAX_LENGTH) {
    return `Group must be at most ${TAG_KEY_MAX_LENGTH} characters.`;
  }
  if (isSystemTagKey(trimmed)) {
    return `Groups can't start with the reserved "${RESERVED_TAG_KEY_PREFIX}" prefix.`;
  }
  if (!TAG_KEY_REGEX.test(trimmed)) {
    return "Use letters, digits, _ . or - (must start with a letter or digit).";
  }
  return null;
}

// Validates a label value. `hasKey` toggles two backend rules: a standalone label
// can't contain ":", and a grouped value can't be exactly "*" (group wildcard).
export function validateTagValue(
  value: string,
  { hasKey }: { hasKey: boolean } = { hasKey: false },
): string | null {
  const trimmed = value.trim();
  if (!trimmed) {
    return "Label is required.";
  }
  if (trimmed.length > TAG_VALUE_MAX_LENGTH) {
    return `Label must be at most ${TAG_VALUE_MAX_LENGTH} characters.`;
  }
  if (trimmed.includes(",")) {
    return "Label can't contain a comma.";
  }
  if (!hasKey && trimmed.includes(":")) {
    return "A label without a group can't contain a colon — add a group (group:label).";
  }
  if (hasKey && trimmed === "*") {
    return 'A grouped value can\'t be exactly "*" (reserved as the group filter wildcard).';
  }
  return null;
}

// Parse a typed `group:label` or bare `label` into a Tag, splitting on the first
// colon so a grouped value may itself contain colons. Null if nothing usable.
export function parseTagInput(raw: string): Tag | null {
  const trimmed = raw.trim();
  if (!trimmed) {
    return null;
  }
  const colon = trimmed.indexOf(":");
  if (colon === -1) {
    return { key: null, value: trimmed };
  }
  const key = trimmed.slice(0, colon).trim();
  const value = trimmed.slice(colon + 1).trim();
  if (!key || !value) {
    return null;
  }
  return { key, value };
}

// Split an in-progress editor/filter query at the first colon into a group key
// and the lowercased value fragment, for autocomplete matching.
export function parseTypedTagQuery(trimmedQuery: string): {
  typedKey: string | null;
  typedValuePartial: string;
} {
  const colonIndex = trimmedQuery.indexOf(":");
  if (colonIndex <= 0) {
    return { typedKey: null, typedValuePartial: "" };
  }
  return {
    typedKey: trimmedQuery.slice(0, colonIndex).trim(),
    typedValuePartial: trimmedQuery
      .slice(colonIndex + 1)
      .trim()
      .toLowerCase(),
  };
}

// Validate a parsed tag (both key — if present — and value). Returns an error
// string or null.
export function validateTag(tag: Tag): string | null {
  if (tag.key !== null) {
    const keyError = validateTagKey(tag.key);
    if (keyError) {
      return keyError;
    }
  }
  return validateTagValue(tag.value, { hasKey: tag.key !== null });
}

// A filter term: value-only {key:null, value:"x"}, group-only {key:"env",
// value:null}, or exact {key:"env", value:"prod"}.
export interface TagFilterTerm {
  key: string | null;
  value: string | null;
}

export function termDedupeKey(term: TagFilterTerm): string {
  return `${term.key ?? ""} ${term.value ?? ""}`;
}

// Parse the `?tags=` param into terms. Forgiving on read (drops malformed/blank
// segments); mirrors the backend, splitting on the first colon (`*` = wildcard).
export function parseTagFilter(
  raw: string | null | undefined,
): Array<TagFilterTerm> {
  if (!raw) {
    return [];
  }
  const terms: Array<TagFilterTerm> = [];
  const seen = new Set<string>();
  for (const segment of raw.split(",")) {
    const term = parseTagFilterTerm(segment);
    if (!term) {
      continue;
    }
    const dedupe = termDedupeKey(term);
    if (seen.has(dedupe)) {
      continue;
    }
    seen.add(dedupe);
    terms.push(term);
  }
  return terms;
}

// Parse a single `?tags=` term. Returns null for malformed/blank input.
export function parseTagFilterTerm(segment: string): TagFilterTerm | null {
  const trimmed = segment.trim();
  if (!trimmed) {
    return null;
  }
  const colon = trimmed.indexOf(":");
  if (colon === -1) {
    // Bare token -> label / value-only.
    return { key: null, value: trimmed };
  }
  const key = trimmed.slice(0, colon).trim();
  const value = trimmed.slice(colon + 1).trim();
  if (!key) {
    return null;
  }
  if (value === "*") {
    return { key, value: null }; // group-only
  }
  if (!value) {
    return null;
  }
  return { key, value };
}

// Canonical serialization: terms sorted (standalone first, then key, then value)
// so identical filters produce one stable, order-independent string.
export function serializeTagFilter(terms: Array<TagFilterTerm>): string {
  return [...terms]
    .sort(
      (a, b) =>
        Number(a.key !== null) - Number(b.key !== null) ||
        (a.key ?? "").localeCompare(b.key ?? "") ||
        (a.value ?? "").localeCompare(b.value ?? ""),
    )
    .map(serializeTagFilterTerm)
    .filter((term) => term !== "")
    .join(",");
}

export function serializeTagFilterTerm(term: TagFilterTerm): string {
  if (term.key === null) {
    return term.value ?? "";
  }
  if (term.value === null) {
    return `${term.key}:*`;
  }
  return `${term.key}:${term.value}`;
}

export function isTag(value: unknown): value is Tag {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const candidate = value as { key?: unknown; value?: unknown };
  return (
    (candidate.key === null || typeof candidate.key === "string") &&
    typeof candidate.value === "string"
  );
}

// Boundary guard for tag payloads. Accepts the current wire shape
// (Array<Tag>) plus the pre-SKY-10683 key->value record, and degrades
// anything else to [] - a shape skew must never throw mid-render (React #31)
// and take the whole route down with the error boundary.
export function normalizeWorkflowTags(input: unknown): Array<Tag> {
  if (Array.isArray(input)) {
    const valid = input.filter(isTag);
    if (valid.length !== input.length) {
      console.warn(
        `[tags] dropped ${input.length - valid.length} malformed tag entries`,
      );
    }
    return valid;
  }
  if (input !== null && typeof input === "object") {
    console.warn("[tags] got a legacy record tag payload; converting");
    return Object.entries(input)
      .filter((entry): entry is [string, string] => {
        return typeof entry[1] === "string";
      })
      .map(([key, value]) => ({ key, value }));
  }
  if (input !== null && input !== undefined) {
    console.warn(`[tags] ignoring tag payload of type ${typeof input}`);
  }
  return [];
}

// Stable React key for a tag. Standalone labels and grouped tags live in
// disjoint namespaces so a null key can't collide with an (invalid) empty key.
export function tagElementKey(tag: Tag): string {
  return tag.key === null
    ? `label:${tag.value}`
    : `group:${tag.key}:${tag.value}`;
}

// Stable order for displaying a list of tags: standalone labels first, then
// grouped by key, then by value. Matches the backend response ordering.
export function sortTags(tags: Array<Tag>): Array<Tag> {
  return [...tags].sort(
    (a, b) =>
      Number(a.key !== null) - Number(b.key !== null) ||
      (a.key ?? "").localeCompare(b.key ?? "") ||
      a.value.localeCompare(b.value),
  );
}
