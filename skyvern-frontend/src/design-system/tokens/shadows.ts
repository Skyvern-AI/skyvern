/**
 * Skyvern Design System - Shadow Tokens
 *
 * Box shadows for creating depth and elevation.
 * Designed to work in both light and dark modes.
 */

/**
 * Base shadow scale
 * From subtle to pronounced elevation
 */
export const shadows = {
  /** No shadow */
  none: "none",
  /** Subtle shadow for slight elevation */
  sm: "0 1px 2px 0 rgb(0 0 0 / 0.05)",
  /** Default shadow for cards and panels */
  DEFAULT: "0 1px 3px 0 rgb(0 0 0 / 0.1), 0 1px 2px -1px rgb(0 0 0 / 0.1)",
  /** Medium shadow for dropdowns */
  md: "0 4px 6px -1px rgb(0 0 0 / 0.1), 0 2px 4px -2px rgb(0 0 0 / 0.1)",
  /** Large shadow for modals and popovers */
  lg: "0 10px 15px -3px rgb(0 0 0 / 0.1), 0 4px 6px -4px rgb(0 0 0 / 0.1)",
  /** Extra large shadow for floating elements */
  xl: "0 20px 25px -5px rgb(0 0 0 / 0.1), 0 8px 10px -6px rgb(0 0 0 / 0.1)",
  /** 2XL shadow for maximum elevation */
  "2xl": "0 25px 50px -12px rgb(0 0 0 / 0.25)",
  /** Inset shadow for pressed states */
  inner: "inset 0 2px 4px 0 rgb(0 0 0 / 0.05)",
} as const;

/**
 * Colored shadows for interactive elements
 * Used for hover/focus states to create glowing effects
 */
export const coloredShadows = {
  /** Primary brand glow */
  primary: "0 0 0 3px hsl(var(--primary) / 0.1)",
  primaryStrong: "0 0 0 3px hsl(var(--primary) / 0.2)",
  /** Success glow */
  success: "0 0 0 3px hsl(var(--success) / 0.15)",
  /** Warning glow */
  warning: "0 0 0 3px hsl(var(--warning) / 0.15)",
  /** Error/destructive glow */
  destructive: "0 0 0 3px hsl(var(--destructive) / 0.15)",
  /** Info glow */
  info: "0 0 0 3px hsl(var(--info) / 0.15)",
} as const;

/**
 * Focus ring shadows
 * Used for keyboard navigation accessibility
 */
export const focusShadows = {
  /** Default focus ring */
  ring: "0 0 0 2px hsl(var(--background)), 0 0 0 4px hsl(var(--ring))",
  /** Focus ring with offset */
  ringOffset: "var(--tw-ring-offset-shadow, 0 0 #0000), var(--tw-ring-shadow, 0 0 #0000)",
  /** Primary focus ring */
  ringPrimary: "0 0 0 2px hsl(var(--background)), 0 0 0 4px hsl(var(--primary))",
  /** Destructive focus ring */
  ringDestructive:
    "0 0 0 2px hsl(var(--background)), 0 0 0 4px hsl(var(--destructive))",
} as const;

/**
 * Semantic shadows for common UI patterns
 */
export const semanticShadows = {
  /** Card shadow */
  card: shadows.DEFAULT,
  /** Dropdown/popover shadow */
  dropdown: shadows.lg,
  /** Modal shadow */
  modal: shadows.xl,
  /** Tooltip shadow */
  tooltip: shadows.md,
  /** Floating action button shadow */
  fab: shadows.lg,
  /** Header/navbar shadow */
  header: shadows.sm,
  /** Sidebar shadow */
  sidebar: shadows.md,
} as const;

export type Shadow = keyof typeof shadows;
export type ColoredShadow = keyof typeof coloredShadows;
export type FocusShadow = keyof typeof focusShadows;
