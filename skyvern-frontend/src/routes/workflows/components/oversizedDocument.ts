export const LARGE_DOCUMENT_CHAR_THRESHOLD = 50_000;
const MAX_STRUCTURE_DEPTH = 200;

function getMaxStructureDepth(text: string, cap: number): number {
  let depth = 0;
  let max = 0;
  let inString = false;
  let escaped = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (inString) {
      if (escaped) {
        escaped = false;
      } else if (ch === "\\") {
        escaped = true;
      } else if (ch === '"') {
        inString = false;
      }
      continue;
    }
    if (ch === '"') {
      inString = true;
    } else if (ch === "{" || ch === "[") {
      depth += 1;
      if (depth > max) {
        max = depth;
        if (max > cap) {
          return max;
        }
      }
    } else if (ch === "}" || ch === "]") {
      if (depth > 0) {
        depth -= 1;
      }
    }
  }
  return max;
}

/**
 * Deeply nested JSON drives CodeMirror's view-layer RangeSet scan into
 * per-level recursion that overflows the call stack.
 */
export function isDeeplyNestedDocument(
  value: string | null | undefined,
): boolean {
  if (typeof value !== "string") {
    return false;
  }
  return getMaxStructureDepth(value, MAX_STRUCTURE_DEPTH) > MAX_STRUCTURE_DEPTH;
}

/**
 * Large or deeply nested documents drive CodeMirror's line-wrapping measurement
 * into per-level recursion / expensive reflow.
 */
export function isOversizedDocument(value: string | null | undefined): boolean {
  if (typeof value !== "string") {
    return false;
  }
  if (value.length > LARGE_DOCUMENT_CHAR_THRESHOLD) {
    return true;
  }
  return isDeeplyNestedDocument(value);
}
