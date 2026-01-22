/**
 * Block label validation utilities
 *
 * Valid block labels:
 * - Start with letter or underscore
 * - Contain only letters, numbers, underscores
 * - Not empty
 * - Not duplicate of existing block
 */

const VALID_BLOCK_LABEL_REGEX = /^[a-zA-Z_][a-zA-Z0-9_]*$/;

interface BlockLabelValidationContext {
  existingLabels: string[];
  currentLabel: string; // To allow keeping the same name
}

/**
 * Validates a block label and returns an error message if invalid
 * @param label The label to validate
 * @param context Contains existing labels and current block's label
 * @returns Error message string if invalid, null if valid
 */
function validateBlockLabel(
  label: string,
  context: BlockLabelValidationContext,
): string | null {
  const trimmed = label.trim();

  if (!trimmed) {
    return "Block name is required";
  }

  if (!VALID_BLOCK_LABEL_REGEX.test(trimmed)) {
    if (/^\d/.test(trimmed)) {
      return "Block name cannot start with a number";
    }
    if (/\s/.test(trimmed)) {
      return "Block name cannot contain spaces. Use underscores instead.";
    }
    if (/-/.test(trimmed)) {
      return "Block name cannot contain dashes. Use underscores instead.";
    }
    return "Block name can only contain letters, numbers, and underscores";
  }

  // Check for duplicates (excluding current block's own label)
  if (trimmed !== context.currentLabel) {
    const isDuplicate = context.existingLabels.some(
      (existing) => existing.toLowerCase() === trimmed.toLowerCase(),
    );
    if (isDuplicate) {
      return `A block named "${trimmed}" already exists`;
    }
  }

  return null; // Valid
}

/**
 * Sanitizes a block label by converting invalid characters
 * @param label The label to sanitize
 * @returns Sanitized label with spaces/dashes converted to underscores
 */
function sanitizeBlockLabel(label: string): string {
  return label.trim().replace(/[\s-]+/g, "_");
}

/**
 * Check if a label is valid without requiring context
 * @param label The label to check
 * @returns true if the format is valid (doesn't check for duplicates)
 */
function isValidBlockLabelFormat(label: string): boolean {
  const trimmed = label.trim();
  return trimmed.length > 0 && VALID_BLOCK_LABEL_REGEX.test(trimmed);
}

export {
  validateBlockLabel,
  sanitizeBlockLabel,
  isValidBlockLabelFormat,
  type BlockLabelValidationContext,
};
