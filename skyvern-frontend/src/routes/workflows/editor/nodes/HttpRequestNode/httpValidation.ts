import { TSON } from "@/util/tson";

// URL Validation Helper
export function validateUrl(url: string): { valid: boolean; message: string } {
  const trimmed = url.trim();
  if (!trimmed) {
    return { valid: false, message: "URL is required" };
  }

  try {
    const parsed = new URL(trimmed);
    if (!["http:", "https:"].includes(parsed.protocol)) {
      return { valid: false, message: "URL must use HTTP or HTTPS protocol" };
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

  const result = TSON.parse(trimmed);
  if (result.success) {
    return { valid: true, message: "Valid JSON" };
  }
  return { valid: false, message: result.error || "Invalid JSON" };
}
