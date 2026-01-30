/**
 * Skyvern Design System
 *
 * A comprehensive design system for building consistent,
 * accessible, and beautiful user interfaces.
 *
 * @example
 * // Import tokens
 * import { colors, typography, spacing } from '@/design-system';
 *
 * @example
 * // Import components
 * import { Spinner, Avatar, StatusBadge } from '@/design-system';
 *
 * @example
 * // Import utilities
 * import { cn } from '@/design-system';
 */

// ============================================
// TOKENS
// ============================================

// All tokens
export * from "./tokens";

// ============================================
// COMPONENTS
// ============================================

// All components
export * from "./components";

// ============================================
// UTILITIES
// ============================================

// Utility functions
export * from "./utils";

// ============================================
// RE-EXPORTS FOR CONVENIENCE
// ============================================

// Theme object (combines all tokens)
export { theme } from "./tokens";

// Commonly used token subsets
export {
  // Colors
  colors,
  statusColors,
  comparisonColors,
  // Typography
  fontFamily,
  fontSize,
  fontWeight,
  textStyles,
  // Spacing
  spacing,
  semanticSpacing,
  zIndex,
  // Shadows
  shadows,
  semanticShadows,
  // Borders
  borderRadius,
  semanticRadius,
  // Animations
  duration,
  easing,
  transitions,
} from "./tokens";
