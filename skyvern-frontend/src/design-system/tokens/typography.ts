/**
 * Skyvern Design System - Typography Tokens
 *
 * Defines font families, sizes, weights, and line heights
 * for consistent text styling across the application.
 */

/**
 * Font family stacks
 */
export const fontFamily = {
  /** Primary sans-serif font for UI text */
  sans: [
    "Inter",
    "ui-sans-serif",
    "system-ui",
    "-apple-system",
    "BlinkMacSystemFont",
    "Segoe UI",
    "Roboto",
    "Helvetica Neue",
    "Arial",
    "sans-serif",
  ],
  /** Monospace font for code and technical content */
  mono: [
    "JetBrains Mono",
    "ui-monospace",
    "SFMono-Regular",
    "SF Mono",
    "Menlo",
    "Consolas",
    "Liberation Mono",
    "monospace",
  ],
} as const;

/**
 * Font sizes with corresponding line heights
 * Based on a modular scale for visual harmony
 */
export const fontSize = {
  /** Extra small - labels, captions */
  xs: { size: "0.75rem", lineHeight: "1rem" }, // 12px / 16px
  /** Small - secondary text, helper text */
  sm: { size: "0.875rem", lineHeight: "1.25rem" }, // 14px / 20px
  /** Base - body text, default */
  base: { size: "1rem", lineHeight: "1.5rem" }, // 16px / 24px
  /** Large - emphasized text */
  lg: { size: "1.125rem", lineHeight: "1.75rem" }, // 18px / 28px
  /** Extra large - subheadings */
  xl: { size: "1.25rem", lineHeight: "1.75rem" }, // 20px / 28px
  /** 2XL - section headings */
  "2xl": { size: "1.5rem", lineHeight: "2rem" }, // 24px / 32px
  /** 3XL - page headings */
  "3xl": { size: "1.875rem", lineHeight: "2.25rem" }, // 30px / 36px
  /** 4XL - large headings */
  "4xl": { size: "2.25rem", lineHeight: "2.5rem" }, // 36px / 40px
  /** 5XL - hero headings */
  "5xl": { size: "3rem", lineHeight: "1" }, // 48px
} as const;

/**
 * Font weights
 */
export const fontWeight = {
  /** Normal weight for body text */
  normal: "400",
  /** Medium weight for slight emphasis */
  medium: "500",
  /** Semibold for headings and labels */
  semibold: "600",
  /** Bold for strong emphasis */
  bold: "700",
} as const;

/**
 * Letter spacing (tracking)
 */
export const letterSpacing = {
  /** Tighter tracking for large headings */
  tighter: "-0.05em",
  /** Slightly tight tracking */
  tight: "-0.025em",
  /** Normal tracking */
  normal: "0",
  /** Wide tracking for small uppercase text */
  wide: "0.025em",
  /** Wider tracking for emphasis */
  wider: "0.05em",
  /** Widest tracking for all-caps labels */
  widest: "0.1em",
} as const;

/**
 * Predefined text styles for common use cases
 * Combines size, weight, and line height for consistency
 */
export const textStyles = {
  /** Page title - largest heading */
  pageTitle: {
    fontSize: fontSize["3xl"].size,
    lineHeight: fontSize["3xl"].lineHeight,
    fontWeight: fontWeight.bold,
    letterSpacing: letterSpacing.tight,
  },
  /** Section heading */
  sectionTitle: {
    fontSize: fontSize["2xl"].size,
    lineHeight: fontSize["2xl"].lineHeight,
    fontWeight: fontWeight.semibold,
    letterSpacing: letterSpacing.tight,
  },
  /** Card or panel title */
  cardTitle: {
    fontSize: fontSize.lg.size,
    lineHeight: fontSize.lg.lineHeight,
    fontWeight: fontWeight.semibold,
  },
  /** Subsection heading */
  subtitle: {
    fontSize: fontSize.base.size,
    lineHeight: fontSize.base.lineHeight,
    fontWeight: fontWeight.medium,
  },
  /** Primary body text */
  body: {
    fontSize: fontSize.base.size,
    lineHeight: fontSize.base.lineHeight,
    fontWeight: fontWeight.normal,
  },
  /** Secondary/smaller body text */
  bodySmall: {
    fontSize: fontSize.sm.size,
    lineHeight: fontSize.sm.lineHeight,
    fontWeight: fontWeight.normal,
  },
  /** Labels and form field labels */
  label: {
    fontSize: fontSize.sm.size,
    lineHeight: fontSize.sm.lineHeight,
    fontWeight: fontWeight.medium,
  },
  /** Helper text and descriptions */
  helper: {
    fontSize: fontSize.sm.size,
    lineHeight: fontSize.sm.lineHeight,
    fontWeight: fontWeight.normal,
  },
  /** Captions and metadata */
  caption: {
    fontSize: fontSize.xs.size,
    lineHeight: fontSize.xs.lineHeight,
    fontWeight: fontWeight.normal,
  },
  /** Code blocks and technical content */
  code: {
    fontSize: fontSize.sm.size,
    lineHeight: fontSize.sm.lineHeight,
    fontWeight: fontWeight.normal,
    fontFamily: fontFamily.mono.join(", "),
  },
  /** Overline text (small uppercase) */
  overline: {
    fontSize: fontSize.xs.size,
    lineHeight: fontSize.xs.lineHeight,
    fontWeight: fontWeight.semibold,
    letterSpacing: letterSpacing.wider,
    textTransform: "uppercase" as const,
  },
} as const;

/**
 * Combined typography export
 */
export const typography = {
  fontFamily,
  fontSize,
  fontWeight,
  letterSpacing,
  textStyles,
} as const;

export type Typography = typeof typography;
export type FontSize = keyof typeof fontSize;
export type FontWeight = keyof typeof fontWeight;
export type TextStyle = keyof typeof textStyles;
