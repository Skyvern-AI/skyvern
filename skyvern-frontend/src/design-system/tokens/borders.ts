/**
 * Skyvern Design System - Border Tokens
 *
 * Border radius and width values for consistent
 * styling of UI elements.
 */

/**
 * Border radius scale
 * Uses CSS variable for base radius to allow customization
 */
export const borderRadius = {
  /** No rounding */
  none: "0",
  /** Subtle rounding - 2px */
  sm: "calc(var(--radius) - 4px)",
  /** Default rounding - 4px */
  DEFAULT: "calc(var(--radius) - 2px)",
  /** Medium rounding - 6px */
  md: "var(--radius)",
  /** Large rounding - 8px */
  lg: "calc(var(--radius) + 2px)",
  /** Extra large rounding - 12px */
  xl: "calc(var(--radius) + 4px)",
  /** 2XL rounding - 16px */
  "2xl": "calc(var(--radius) + 8px)",
  /** 3XL rounding - 24px */
  "3xl": "calc(var(--radius) + 16px)",
  /** Fully rounded (pill shape) */
  full: "9999px",
} as const;

/**
 * Border width scale
 */
export const borderWidth = {
  /** No border */
  0: "0px",
  /** Default border - 1px */
  DEFAULT: "1px",
  /** Medium border - 2px */
  2: "2px",
  /** Thick border - 4px */
  4: "4px",
  /** Extra thick border - 8px */
  8: "8px",
} as const;

/**
 * Semantic border radii for common UI patterns
 */
export const semanticRadius = {
  /** Button border radius */
  button: borderRadius.md,
  /** Small button border radius */
  buttonSm: borderRadius.sm,
  /** Large button border radius */
  buttonLg: borderRadius.lg,
  /** Input field border radius */
  input: borderRadius.md,
  /** Card border radius */
  card: borderRadius.xl,
  /** Dialog/modal border radius */
  dialog: borderRadius["2xl"],
  /** Dropdown menu border radius */
  dropdown: borderRadius.lg,
  /** Badge/tag border radius */
  badge: borderRadius.full,
  /** Avatar border radius */
  avatar: borderRadius.full,
  /** Tooltip border radius */
  tooltip: borderRadius.md,
  /** Checkbox border radius */
  checkbox: borderRadius.sm,
  /** Switch track border radius */
  switch: borderRadius.full,
} as const;

/**
 * Outline styles for focus states
 */
export const outlineStyles = {
  /** No outline */
  none: {
    outlineStyle: "none" as const,
  },
  /** Default focus outline */
  focus: {
    outlineWidth: "2px",
    outlineStyle: "solid" as const,
    outlineColor: "hsl(var(--ring))",
    outlineOffset: "2px",
  },
  /** Primary focus outline */
  focusPrimary: {
    outlineWidth: "2px",
    outlineStyle: "solid" as const,
    outlineColor: "hsl(var(--primary))",
    outlineOffset: "2px",
  },
  /** Destructive focus outline */
  focusDestructive: {
    outlineWidth: "2px",
    outlineStyle: "solid" as const,
    outlineColor: "hsl(var(--destructive))",
    outlineOffset: "2px",
  },
} as const;

/**
 * Divider styles
 */
export const dividerStyles = {
  /** Horizontal divider */
  horizontal: {
    height: "1px",
    width: "100%",
    backgroundColor: "hsl(var(--border))",
  },
  /** Vertical divider */
  vertical: {
    width: "1px",
    height: "100%",
    backgroundColor: "hsl(var(--border))",
  },
} as const;

export type BorderRadius = keyof typeof borderRadius;
export type BorderWidth = keyof typeof borderWidth;
export type SemanticRadius = keyof typeof semanticRadius;
