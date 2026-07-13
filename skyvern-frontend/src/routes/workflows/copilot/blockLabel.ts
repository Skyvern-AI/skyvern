// Copilot block labels are LLM-authored Python identifiers (snake_case,
// occasionally with a trailing retry-count version suffix like `_v2`) —
// readable to whoever wrote the code, not to the person watching the build.
// This derives a display label; callers keep the raw `label` around (e.g. in
// a `title` attribute) for debugging and must keep using it for identity
// (canvas selection, keys) since only the rendered text should change.
const VERSION_SUFFIX_RE = /_v\d+$/i;

export function humanizeBlockLabel(label: string): string {
  const words = label
    .replace(VERSION_SUFFIX_RE, "")
    .split(/[_\s]+/)
    .filter(Boolean);
  if (words.length === 0) return label;
  return words.map((word) => word[0]!.toUpperCase() + word.slice(1)).join(" ");
}
