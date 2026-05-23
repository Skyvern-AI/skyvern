import type { Node } from "@xyflow/react";

/**
 * Pure helpers for detecting, transforming, and walking Jinja-style
 * (`{{ key }}`) references inside block data. Kept in a standalone module so
 * test code — and sibling editor utilities that only need the reference
 * logic — can import these without transitively loading the full editor
 * runtime (node type registry, AxiosClient, React Query hooks, etc.).
 *
 * Behavior is unchanged from the prior `workflowEditorUtils.ts` home; see
 * git history on that file for the origin of each function.
 */

/**
 * Escapes special regex characters in a string.
 */
function escapeRegExp(string: string): string {
  return string.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * Replaces jinja-style references to a variable name.
 * Handles patterns like {{oldKey}}, {{oldKey.field}}, {{oldKey | filter}}
 * @param text - The text to search in
 * @param oldKey - The key to replace (without braces)
 * @param newKey - The new key to use (without braces)
 * @returns The text with references replaced
 */
export function replaceJinjaReference(
  text: string,
  oldKey: string,
  newKey: string,
): string {
  // Match {{oldKey}} or {{oldKey.something}} or {{oldKey | filter}} or {{oldKey[0]}} etc.
  // Use negative lookahead to ensure key is not followed by identifier characters,
  // which prevents matching {{keyOther}} when searching for {{key}}
  // Capture whitespace after {{ to preserve formatting (e.g., "{{ key }}" stays "{{ newKey }}")
  const regex = new RegExp(
    `\\{\\{(\\s*)${escapeRegExp(oldKey)}(?![a-zA-Z0-9_])`,
    "g",
  );
  return text.replace(regex, (_, whitespace) => `{{${whitespace}${newKey}`);
}

/**
 * Removes jinja-style references from a string.
 * Handles patterns like {{key}}, {{key.field}}, {{key | filter}}
 * @param text - The text to search in
 * @param key - The key to remove (without braces)
 * @returns The text with references removed
 */
// Cap the inside-braces match at 500 chars: bounded prevents catastrophic
// backtracking on adversarial input. Real Jinja expressions in this editor
// (`{{ key.field | filter:arg }}`) are well under that bound; on the rare
// occurrence that one exceeds it, the reference simply stays in place
// rather than the regex engine stalling. The deletion path is best-effort:
// callers don't depend on every reference being removed.
const MAX_JINJA_INTERIOR_CHARS = 500;

export function removeJinjaReference(text: string, key: string): string {
  // Capture whitespace adjacent to the reference so the replacement can
  // collapse the gap left behind without globally rewriting the field.
  // Globally collapsing was a real bug: a multiline prompt with
  // intentional `\n\n` paragraph breaks would have those breaks flattened
  // every time an unrelated block/parameter was deleted.
  // Interior pattern allows a single `}` (e.g. `default('{}')`); only
  // `}}` terminates, since that is the closing delimiter.
  const regex = new RegExp(
    `(\\s*)\\{\\{\\s*${escapeRegExp(key)}(?![a-zA-Z0-9_])(?:[^}]|}(?!})){0,${MAX_JINJA_INTERIOR_CHARS}}\\}\\}(\\s*)`,
    "g",
  );
  return text.replace(
    regex,
    (match, leading: string, trailing: string, offset: number) => {
      const atStart = offset === 0;
      const atEnd = offset + match.length === text.length;
      if (atStart && atEnd) return "";
      // At an edge, preserve the user's outer whitespace; only the
      // captured-on-the-other-side run is dropped along with the reference.
      if (atStart) return leading;
      if (atEnd) return trailing;
      // Mid-string: collapse the gap to a single separator so words/lines
      // adjoining the removed reference don't fuse. Prefer a newline when
      // either side had one so multi-line layout is preserved.
      if (leading.includes("\n") || trailing.includes("\n")) return "\n";
      if (leading.length > 0 || trailing.length > 0) return " ";
      return "";
    },
  );
}

/**
 * Checks if a string contains a jinja reference to a specific key.
 */
export function containsJinjaReference(text: string, key: string): boolean {
  // Use negative lookahead to ensure key is not followed by identifier characters
  const regex = new RegExp(`\\{\\{\\s*${escapeRegExp(key)}(?![a-zA-Z0-9_])`);
  return regex.test(text);
}

/**
 * Recursively checks if any string field in an object contains a jinja
 * reference to a key. Mirrors the shape used by the editor's existing ref
 * scanner so nested payloads (loop prompts, HTTP headers, etc.) are covered
 * — not just top-level string fields.
 */
export function objectContainsJinjaReference(
  obj: unknown,
  key: string,
  skipKeys: Set<string>,
  depth: number = 0,
): boolean {
  const MAX_DEPTH = 50;
  if (depth > MAX_DEPTH || obj === null || obj === undefined) {
    return false;
  }

  if (typeof obj === "string") {
    return containsJinjaReference(obj, key);
  }

  if (Array.isArray(obj)) {
    return obj.some((item) =>
      objectContainsJinjaReference(item, key, skipKeys, depth + 1),
    );
  }

  if (typeof obj === "object") {
    for (const [objKey, value] of Object.entries(obj)) {
      if (skipKeys.has(objKey)) {
        continue;
      }
      if (objectContainsJinjaReference(value, key, skipKeys, depth + 1)) {
        return true;
      }
    }
  }

  return false;
}

// Keys to skip when checking for jinja references (same as transform).
export const SKIP_KEYS_FOR_JINJA_CHECK = new Set([
  "label",
  "key",
  "type",
  "id",
  "nodeId",
  "parameterKeys",
]);

/**
 * Information about a block that references a parameter or block output.
 */
export type AffectedBlock = {
  nodeId: string;
  label: string;
  hasParameterKeyReference: boolean;
  hasJinjaReference: boolean;
};

/**
 * Finds all blocks that reference a given key (parameter or block output).
 * Checks both parameterKeys arrays and jinja references in text fields.
 */
export function getAffectedBlocks<T extends Node>(
  nodes: T[],
  key: string,
): AffectedBlock[] {
  const affectedBlocks: AffectedBlock[] = [];

  for (const node of nodes) {
    // Skip non-block nodes (start, nodeAdder, etc.)
    if (
      !node.data ||
      !("label" in node.data) ||
      node.type === "start" ||
      node.type === "nodeAdder"
    ) {
      continue;
    }

    const label = node.data.label as string;
    let hasParameterKeyReference = false;
    let hasJinjaReference = false;

    const parameterKeys = node.data.parameterKeys as Array<string> | undefined;
    if (parameterKeys?.includes(key)) {
      hasParameterKeyReference = true;
    }

    if (node.type === "loop") {
      const loopVarRef = node.data.loopVariableReference as string | undefined;
      if (loopVarRef === key || containsJinjaReference(loopVarRef ?? "", key)) {
        hasJinjaReference = true;
      }
    }

    if (
      objectContainsJinjaReference(node.data, key, SKIP_KEYS_FOR_JINJA_CHECK)
    ) {
      hasJinjaReference = true;
    }

    if (hasParameterKeyReference || hasJinjaReference) {
      affectedBlocks.push({
        nodeId: node.id,
        label,
        hasParameterKeyReference,
        hasJinjaReference,
      });
    }
  }

  return affectedBlocks;
}
