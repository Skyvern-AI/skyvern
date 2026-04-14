import { linter, type Diagnostic } from "@codemirror/lint";

/**
 * Scan a JSON string for **top-level** object-key literals (`"KEY":` where
 * KEY sits directly inside the outermost `{…}`) and return the `[from, to)`
 * character range + parsed content for each one.
 *
 * Scope is restricted to the top level on purpose: save-time validation
 * (`validateErrorCodeMapping`) only iterates `Object.keys(parsed)` at depth
 * 1, so the inline linter must use the same scope — otherwise a nested key
 * like `{"ERR": {" BAD": "x"}}` would get an in-editor squiggle but save
 * cleanly, which is worse than no diagnostic at all. Codex review on
 * #10116.
 *
 * This is a hand-rolled character walk rather than a full JSON parse
 * because we need character positions even when the JSON is mid-edit and
 * not yet well-formed. The walker tracks brace depth (ignoring braces
 * inside string literals) and only emits a key when the brace depth
 * transition lands at depth 1.
 *
 * Return type: `from`/`to` are offsets into the source string; `raw` is the
 * literal with its surrounding quotes stripped AND escape sequences
 * unescaped, so `" FOO"` becomes ` FOO` ready for a `trim()` check.
 */
export function scanJsonKeys(
  source: string,
): Array<{ from: number; to: number; raw: string }> {
  const keys: Array<{ from: number; to: number; raw: string }> = [];
  let depth = 0;
  let i = 0;
  const len = source.length;

  while (i < len) {
    const ch = source[i]!;

    if (ch === "{") {
      depth++;
      i++;
      continue;
    }
    if (ch === "}") {
      depth--;
      i++;
      continue;
    }
    if (ch !== '"') {
      i++;
      continue;
    }

    // At a string-literal opener. Walk to the matching closing quote,
    // honoring backslash escapes.
    const stringStart = i;
    i++;
    while (i < len) {
      const c = source[i]!;
      if (c === "\\") {
        i += 2;
        continue;
      }
      if (c === '"') {
        break;
      }
      i++;
    }
    if (i >= len) {
      // Unterminated string — stop scanning; nothing sensible left to
      // report.
      break;
    }
    const stringEnd = i + 1; // include the closing quote
    i++;

    // This string counts as an object key iff:
    //   - the containing brace depth is exactly 1 (we are inside the
    //     outermost `{`), and
    //   - the next non-whitespace character is a colon.
    if (depth !== 1) {
      continue;
    }
    let j = i;
    while (j < len && /\s/.test(source[j]!)) {
      j++;
    }
    if (source[j] !== ":") {
      continue;
    }

    const literal = source.slice(stringStart, stringEnd);
    let raw: string;
    try {
      raw = JSON.parse(literal);
    } catch {
      // Shouldn't happen — we just walked a well-formed JSON string
      // literal — but fall back to stripping the surrounding quotes.
      raw = literal.slice(1, -1);
    }
    keys.push({ from: stringStart, to: stringEnd, raw });
  }

  return keys;
}

/**
 * CodeMirror linter extension for the `error_code_mapping` editor. Emits a
 * Diagnostic on every key literal whose content has surrounding whitespace,
 * rendered as a squiggly underline on the exact character range plus a
 * gutter marker + hover tooltip. This is the in-editor analogue of the
 * summary box rendered by ErrorCodeMappingValidation — same problem set,
 * pinpointed to the offending line.
 *
 * Parse errors are NOT reported here — CodeMirror's built-in JSON parser
 * already highlights syntactic errors. We only add the semantic layer
 * (whitespace-bearing keys) on top.
 */
export const errorCodeMappingLinter = linter((view) => {
  const diagnostics: Diagnostic[] = [];
  const source = view.state.doc.toString();
  if (!source || source === "null") {
    return diagnostics;
  }
  for (const key of scanJsonKeys(source)) {
    if (key.raw !== key.raw.trim()) {
      diagnostics.push({
        from: key.from,
        to: key.to,
        severity: "error",
        message: `Key "${key.raw}" has surrounding whitespace — remove it or this error code will never match at runtime.`,
        source: "error_code_mapping",
      });
    }
  }
  return diagnostics;
});
