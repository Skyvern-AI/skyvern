type JsonLocation = {
  position: number;
  line: number;
  column: number;
};

const allowedJsonWhitespace = new Set([" ", "\n", "\r", "\t"]);

const unsupportedWhitespaceNames: Record<string, string> = {
  "\u00a0": "non-breaking space",
  "\u1680": "ogham space mark",
  "\u2000": "en quad",
  "\u2001": "em quad",
  "\u2002": "en space",
  "\u2003": "em space",
  "\u2004": "three-per-em space",
  "\u2005": "four-per-em space",
  "\u2006": "six-per-em space",
  "\u2007": "figure space",
  "\u2008": "punctuation space",
  "\u2009": "thin space",
  "\u200a": "hair space",
  "\u2028": "line separator",
  "\u2029": "paragraph separator",
  "\u202f": "narrow non-breaking space",
  "\u205f": "medium mathematical space",
  "\u3000": "ideographic space",
  "\ufeff": "byte order mark",
};

function getLocation(input: string, position: number): JsonLocation {
  let line = 1;
  let column = 1;

  for (let i = 0; i < position; i++) {
    if (input.charAt(i) === "\n") {
      line++;
      column = 1;
    } else {
      column++;
    }
  }

  return { position, line, column };
}

function getCodePointLabel(char: string): string {
  const codePoint = char.codePointAt(0) ?? char.charCodeAt(0);
  return `U+${codePoint.toString(16).toUpperCase().padStart(4, "0")}`;
}

function getErrorMessage(error: unknown): string {
  if (error instanceof Error) {
    return error.message;
  }
  if (typeof error === "string") {
    return error;
  }
  return String(error);
}

function getUnsupportedWhitespace(input: string): {
  char: string;
  location: JsonLocation;
} | null {
  let inString = false;
  let escapeNext = false;

  for (let i = 0; i < input.length; i++) {
    const char = input.charAt(i);

    if (escapeNext) {
      escapeNext = false;
      continue;
    }

    if (char === "\\") {
      escapeNext = true;
      continue;
    }

    if (char === '"') {
      inString = !inString;
      continue;
    }

    if (!inString && !allowedJsonWhitespace.has(char) && /\s/u.test(char)) {
      return { char, location: getLocation(input, i) };
    }
  }

  return null;
}

function addLocationFromPosition(input: string, message: string): string {
  if (/line \d+ column \d+/i.test(message)) {
    return message;
  }

  const positionMatch = message.match(/position (\d+)/i);
  if (!positionMatch) {
    return message;
  }

  const position = Number(positionMatch[1]);
  if (!Number.isFinite(position)) {
    return message;
  }

  const { line, column } = getLocation(input, position);
  return `${message} (line ${line} column ${column})`;
}

export function getJsonParseErrorDetail(input: string, error: unknown): string {
  const message = getErrorMessage(error) || "Parse error";
  const unsupportedWhitespace = getUnsupportedWhitespace(input);

  if (unsupportedWhitespace) {
    const name =
      unsupportedWhitespaceNames[unsupportedWhitespace.char] ??
      "unsupported whitespace";
    const codePoint = getCodePointLabel(unsupportedWhitespace.char);
    const { line, column } = unsupportedWhitespace.location;

    return `JSON contains a ${name} (${codePoint}) at line ${line}, column ${column}; replace it with a regular space. Parser detail: ${addLocationFromPosition(input, message)}`;
  }

  return addLocationFromPosition(input, message);
}

export function getInvalidJsonMessage(input: string, error: unknown): string {
  return `Invalid JSON: ${getJsonParseErrorDetail(input, error)}`;
}
