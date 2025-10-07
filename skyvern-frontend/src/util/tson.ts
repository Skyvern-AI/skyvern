type JSONValue =
  | string
  | number
  | boolean
  | null
  | JSONValue[]
  | { [key: string]: JSONValue };

interface ParseResult {
  success: boolean;
  data?: JSONValue;
  error?: string;
}

const placeholder = () => "<STUB>";

/**
 * TSON ("templated JSON") is a superset of JSON, where double curly braces {{...}} can:
 *  - exist anywhere outside of string literals, and
 *  - are treated as placeholders
 */
const TSON = {
  parse(input: string): ParseResult {
    try {
      const balanceCheck = checkDoubleBraceBalance(input);

      if (!balanceCheck.balanced) {
        return {
          success: false,
          error: balanceCheck.error,
        };
      }

      const pipeline = [
        replaceBracesOutsideQuotes,
        // removeTrailingCommas,
        JSON.parse,
      ];

      const parsed = pipeline.reduce((acc, fn) => fn(acc), input) as JSONValue;

      return {
        success: true,
        data: parsed,
      };
    } catch (error) {
      return {
        success: false,
        error: error instanceof Error ? error.message : String(error),
      };
    }
  },
};

function checkDoubleBraceBalance(input: string): {
  balanced: boolean;
  error?: string;
} {
  let inString = false;
  let escapeNext = false;
  let depth = 0;

  for (let i = 0; i < input.length; i++) {
    const char = input[i];
    const nextChar = input[i + 1];

    // handle escape sequences
    if (escapeNext) {
      escapeNext = false;
      continue;
    }

    if (char === "\\") {
      escapeNext = true;
      continue;
    }

    // inside-string tracking
    if (char === '"') {
      inString = !inString;
      continue;
    }

    // double braces counts (only outside strings)
    if (!inString) {
      if (char === "{" && nextChar === "{") {
        depth++;
        i++; // skip next char
      } else if (char === "}" && nextChar === "}") {
        depth--;
        if (depth < 0) {
          return {
            balanced: false,
            error: `Unmatched closing }} at position ${i}`,
          };
        }
        i++; // skip next char
      }
    }
  }

  if (depth > 0) {
    return {
      balanced: false,
      error: `Unclosed {{ - missing ${depth} closing }}`,
    };
  }

  return { balanced: true };
}

function replaceBracesOutsideQuotes(input: string): string {
  let result = "";
  let inString = false;
  let escapeNext = false;
  let inDoubleBrace = 0; // track nesting depth of {{...}}

  for (let i = 0; i < input.length; i++) {
    const char = input[i];
    const nextChar = input[i + 1];

    // escape sequences
    if (escapeNext) {
      if (inDoubleBrace === 0) {
        result += char;
      }
      escapeNext = false;
      continue;
    }

    if (char === "\\") {
      if (inDoubleBrace === 0) {
        result += char;
      }
      escapeNext = true;
      continue;
    }

    // inside-string tracking
    if (char === '"') {
      inString = !inString;
      if (inDoubleBrace === 0) {
        result += char;
      }
      continue;
    }

    // double braces (only outside strings)
    if (!inString) {
      if (char === "{" && nextChar === "{") {
        if (inDoubleBrace === 0) {
          result += `"${placeholder()}"`;
        }
        inDoubleBrace++;
        i++; // skip next char
        continue;
      } else if (char === "}" && nextChar === "}") {
        inDoubleBrace--;
        i++; // skip next char
        continue;
      }
    }

    // add characters when we're not inside double braces
    if (inDoubleBrace === 0) {
      result += char;
    }
  }

  return result;
}

export function removeTrailingCommas(input: string): string {
  let result = "";
  let inString = false;
  let escapeNext = false;

  for (let i = 0; i < input.length; i++) {
    const char = input[i];

    // escape sequences
    if (escapeNext) {
      result += char;
      escapeNext = false;
      continue;
    }

    if (char === "\\") {
      result += char;
      escapeNext = true;
      continue;
    }

    // inside-string tracking
    if (char === '"') {
      inString = !inString;
      result += char;
      continue;
    }

    // check for trailing commas (outside strings)
    if (!inString && char === ",") {
      // look-ahead for the next non-whitespace character
      let j = i + 1;
      // eslint-disable-next-line @typescript-eslint/ban-ts-comment
      // @ts-ignore
      while (j < input.length && /\s/.test(input[j])) {
        j++;
      }

      // if next non-whitespace is } or ], skip the comma
      if (j < input.length && (input[j] === "}" || input[j] === "]")) {
        continue; // Skip this comma
      }
    }

    result += char;
  }

  return result;
}

export { TSON };
