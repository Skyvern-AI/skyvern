/**
 * Skyvern Design System - Token Barrel Export
 *
 * Central export point for all design tokens.
 * Import from here for access to the complete design system.
 */

// Individual token exports
export * from "./colors";
export * from "./typography";
export * from "./spacing";
export * from "./shadows";
export * from "./borders";
export * from "./animations";

// Import for combined theme object
import { colors, statusColors, comparisonColors } from "./colors";
import { typography, fontFamily, fontSize, fontWeight, textStyles } from "./typography";
import { spacing, semanticSpacing, containerWidths, zIndex } from "./spacing";
import { shadows, coloredShadows, focusShadows, semanticShadows } from "./shadows";
import { borderRadius, borderWidth, semanticRadius } from "./borders";
import { animations, duration, easing, transitions, animationPresets } from "./animations";

/**
 * Complete theme object
 * Contains all design tokens organized by category
 */
export const theme = {
  colors,
  typography,
  spacing,
  shadows,
  borders: {
    radius: borderRadius,
    width: borderWidth,
    semantic: semanticRadius,
  },
  animations,
} as const;

/**
 * Flat exports for convenience
 * Use these for quick access to common tokens
 */
export {
  // Colors
  statusColors,
  comparisonColors,
  // Typography
  fontFamily,
  fontSize,
  fontWeight,
  textStyles,
  // Spacing
  semanticSpacing,
  containerWidths,
  zIndex,
  // Shadows
  coloredShadows,
  focusShadows,
  semanticShadows,
  // Borders
  borderRadius,
  borderWidth,
  semanticRadius,
  // Animations
  duration,
  easing,
  transitions,
  animationPresets,
};

export type Theme = typeof theme;
