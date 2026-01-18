export function validateBitwardenLoginCredential(
  collectionId: string | null,
  itemId: string | null,
  urlParameterKey: string | null,
): string | null {
  if (!collectionId && !itemId) {
    return "Collection ID or Item ID is required";
  }
  if (collectionId && !urlParameterKey) {
    return "URL Parameter Key is required when collection ID is used";
  }
  return null;
}

/**
 * Valid Jinja variable names:
 * - Start with letter or underscore
 * - Contain only letters, numbers, underscores
 * - No spaces, slashes, dashes, dots, or other special chars
 */
const VALID_PARAMETER_NAME_REGEX = /^[a-zA-Z_][a-zA-Z0-9_]*$/;

const JINJA_RESERVED_KEYWORDS = [
  "true",
  "false",
  "null",
  "none",
  "and",
  "or",
  "not",
  "in",
  "is",
];

/**
 * Validates a parameter name for use in Jinja templates.
 * Returns an error message if invalid, or null if valid.
 */
export function validateParameterName(name: string): string | null {
  // Check for empty
  if (!name || name.trim() === "") {
    return "Parameter name is required";
  }

  const trimmedName = name.trim();

  // Check for reserved keywords
  if (JINJA_RESERVED_KEYWORDS.includes(trimmedName.toLowerCase())) {
    return `"${trimmedName}" is a reserved keyword`;
  }

  // Check format with specific error messages
  if (!VALID_PARAMETER_NAME_REGEX.test(trimmedName)) {
    if (/^\d/.test(trimmedName)) {
      return "Parameter name cannot start with a number";
    }
    if (/\s/.test(trimmedName)) {
      return "Parameter name cannot contain spaces. Use underscores instead.";
    }
    if (/[/\\]/.test(trimmedName)) {
      return "Parameter name cannot contain slashes";
    }
    if (/-/.test(trimmedName)) {
      return "Parameter name cannot contain dashes. Use underscores instead.";
    }
    if (/\./.test(trimmedName)) {
      return "Parameter name cannot contain dots";
    }
    return "Parameter name can only contain letters, numbers, and underscores";
  }

  return null;
}
