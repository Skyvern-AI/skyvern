type DocTextPart = string | { ref: string };

export function splitJinjaReferences(value: string): DocTextPart[] {
  const parts: DocTextPart[] = [];
  let cursor = 0;

  while (cursor < value.length) {
    const open = value.indexOf("{{", cursor);
    if (open < 0) {
      parts.push(value.slice(cursor));
      break;
    }
    const close = value.indexOf("}}", open + 2);
    if (close < 0) {
      parts.push(value.slice(cursor));
      break;
    }
    parts.push(value.slice(cursor, open), {
      ref: value.slice(open + 2, close).trim(),
    });
    cursor = close + 2;
  }

  if (!parts.length) parts.push(value);
  return parts;
}

export function stripJinjaReferences(value: string): string {
  return splitJinjaReferences(value)
    .map((part) => (typeof part === "string" ? part : part.ref))
    .join("");
}
