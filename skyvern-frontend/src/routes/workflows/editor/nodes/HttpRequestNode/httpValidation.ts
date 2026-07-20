import { TSON } from "@/util/tson";
import { getInvalidJsonMessage } from "@/util/jsonParseError";
import { isTemplateExpression } from "@/util/googleSheetsUrl";

const HTTP_SCHEME_REQUIRED_MESSAGE = "URL must use HTTP or HTTPS protocol";

// URL Validation Helper
export function validateUrl(url: string): { valid: boolean; message: string } {
  const trimmed = url.trim();
  if (!trimmed) {
    return { valid: false, message: "URL is required" };
  }

  const schemeProbe = trimmed.replace(/\s/g, "");
  const explicitScheme = schemeProbe
    .match(/^([a-zA-Z][a-zA-Z0-9+.-]*):/)?.[1]
    ?.toLowerCase();
  if (explicitScheme && !["http", "https"].includes(explicitScheme)) {
    return { valid: false, message: HTTP_SCHEME_REQUIRED_MESSAGE };
  }

  if (schemeProbe.startsWith("//")) {
    return { valid: false, message: "Invalid URL format" };
  }

  if (isTemplateExpression(trimmed)) {
    return { valid: true, message: "Valid URL" };
  }

  try {
    const parsed = new URL(trimmed);
    if (!["http:", "https:"].includes(parsed.protocol)) {
      return { valid: false, message: HTTP_SCHEME_REQUIRED_MESSAGE };
    }
    return { valid: true, message: "Valid URL" };
  } catch {
    return { valid: false, message: "Invalid URL format" };
  }
}

// JSON Validation Helper
export function validateJson(value: string): {
  valid: boolean;
  message: string | null;
} {
  const trimmed = value.trim();
  if (!trimmed || trimmed === "{}" || trimmed === "[]") {
    return { valid: true, message: null };
  }

  // First check: does it pass TSON (template-aware) parsing?
  const tsonResult = TSON.parse(trimmed);
  if (!tsonResult.success) {
    return {
      valid: false,
      message: getInvalidJsonMessage(
        trimmed,
        tsonResult.error || "Parse error",
      ),
    };
  }

  // Second check: does it also pass strict JSON parsing?
  // If TSON passes but JSON.parse fails, the input contains unquoted {{ }} placeholders.
  // The backend expects template placeholders to be quoted strings, so this is always an error.
  try {
    JSON.parse(trimmed);
  } catch {
    return {
      valid: false,
      message:
        'Template placeholders must be wrapped in quotes, e.g. "{{ parameter }}"',
    };
  }

  return { valid: true, message: "Valid JSON" };
}
