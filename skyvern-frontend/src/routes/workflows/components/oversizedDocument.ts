const LARGE_DOCUMENT_CHAR_THRESHOLD = 50_000;
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
 * Large or deeply nested documents drive CodeMirror's view-layer RangeSet scan
 * and line-wrapping measurement into per-level recursion that overflows the
 * call stack (SKY-11432). Above these bounds the editor falls back to plain,
 * unwrapped text instead of syntax-highlighted, wrapped rendering.
 */
export function isOversizedDocument(value: string): boolean {
  if (value.length > LARGE_DOCUMENT_CHAR_THRESHOLD) {
    return true;
  }
  return getMaxStructureDepth(value, MAX_STRUCTURE_DEPTH) > MAX_STRUCTURE_DEPTH;
}
