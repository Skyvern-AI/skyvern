/**
 * Skyvern Design System - Color Tokens
 *
 * Colors are defined in HSL format to align with existing CSS variables.
 * Use these tokens to ensure consistency across the application.
 */

/**
 * Brand colors for Skyvern identity
 */
export const brandColors = {
  /** Primary brand color - used for main CTAs and active states */
  primary: {
    DEFAULT: "hsl(var(--primary))",
    foreground: "hsl(var(--primary-foreground))",
  },
  /** Secondary brand color - used for supporting elements */
  secondary: {
    DEFAULT: "hsl(var(--secondary))",
    foreground: "hsl(var(--secondary-foreground))",
  },
  /** Tertiary brand color - used for muted accents */
  tertiary: {
    DEFAULT: "hsl(var(--tertiary))",
    foreground: "hsl(var(--tertiary-foreground))",
  },
} as const;

/**
 * Semantic colors for conveying meaning
 */
export const semanticColors = {
  /** Success states - completed, verified, active */
  success: {
    DEFAULT: "hsl(var(--success))",
    foreground: "hsl(var(--success-foreground))",
    light: "hsl(var(--success) / 0.1)",
    muted: "hsl(var(--success) / 0.4)",
  },
  /** Warning states - requires attention, pending review */
  warning: {
    DEFAULT: "hsl(var(--warning))",
    foreground: "hsl(var(--warning-foreground))",
    light: "hsl(var(--warning) / 0.1)",
    muted: "hsl(var(--warning) / 0.4)",
  },
  /** Error/destructive states - failed, errors, delete actions */
  destructive: {
    DEFAULT: "hsl(var(--destructive))",
    foreground: "hsl(var(--destructive-foreground))",
    light: "hsl(var(--destructive) / 0.1)",
    muted: "hsl(var(--destructive) / 0.4)",
  },
  /** Info states - informational, neutral highlights */
  info: {
    DEFAULT: "hsl(var(--info))",
    foreground: "hsl(var(--info-foreground))",
    light: "hsl(var(--info) / 0.1)",
    muted: "hsl(var(--info) / 0.4)",
  },
} as const;

/**
 * Status colors for workflow and task states
 * Maps directly to Status enum values from api/types.ts
 */
export const statusColors = {
  /** Created - initial state, not yet started */
  created: {
    bg: "hsl(var(--muted))",
    text: "hsl(var(--muted-foreground))",
    border: "hsl(var(--border))",
    dot: "hsl(var(--muted-foreground))",
  },
  /** Running - currently executing */
  running: {
    bg: "hsl(var(--info) / 0.1)",
    text: "hsl(var(--info))",
    border: "hsl(var(--info) / 0.3)",
    dot: "hsl(var(--info))",
  },
  /** Queued - waiting to execute */
  queued: {
    bg: "hsl(var(--warning) / 0.1)",
    text: "hsl(var(--warning))",
    border: "hsl(var(--warning) / 0.3)",
    dot: "hsl(var(--warning))",
  },
  /** Completed - successfully finished */
  completed: {
    bg: "hsl(var(--success) / 0.1)",
    text: "hsl(var(--success))",
    border: "hsl(var(--success) / 0.3)",
    dot: "hsl(var(--success))",
  },
  /** Failed - encountered an error */
  failed: {
    bg: "hsl(var(--destructive) / 0.1)",
    text: "hsl(var(--destructive))",
    border: "hsl(var(--destructive) / 0.3)",
    dot: "hsl(var(--destructive))",
  },
  /** Terminated - manually stopped */
  terminated: {
    bg: "hsl(var(--destructive) / 0.1)",
    text: "hsl(var(--destructive))",
    border: "hsl(var(--destructive) / 0.3)",
    dot: "hsl(var(--destructive))",
  },
  /** Canceled - user cancelled */
  canceled: {
    bg: "hsl(var(--muted))",
    text: "hsl(var(--muted-foreground))",
    border: "hsl(var(--border))",
    dot: "hsl(var(--muted-foreground))",
  },
  /** Timed out - exceeded time limit */
  timed_out: {
    bg: "hsl(var(--warning) / 0.1)",
    text: "hsl(var(--warning))",
    border: "hsl(var(--warning) / 0.3)",
    dot: "hsl(var(--warning))",
  },
  /** Skipped - intentionally not executed */
  skipped: {
    bg: "hsl(var(--muted))",
    text: "hsl(var(--muted-foreground))",
    border: "hsl(var(--border))",
    dot: "hsl(var(--muted-foreground))",
  },
  /** Paused - temporarily halted */
  paused: {
    bg: "hsl(var(--warning) / 0.1)",
    text: "hsl(var(--warning))",
    border: "hsl(var(--warning) / 0.3)",
    dot: "hsl(var(--warning))",
  },
} as const;

/**
 * Comparison colors for diff views
 * Used in workflow comparison panels
 */
export const comparisonColors = {
  /** Added - new content */
  added: {
    bg: "hsl(142 76% 36% / 0.15)",
    text: "hsl(142 76% 36%)",
    border: "hsl(142 76% 36% / 0.3)",
  },
  /** Modified - changed content */
  modified: {
    bg: "hsl(45 93% 47% / 0.15)",
    text: "hsl(45 93% 47%)",
    border: "hsl(45 93% 47% / 0.3)",
  },
  /** Removed - deleted content */
  removed: {
    bg: "hsl(21 90% 48% / 0.15)",
    text: "hsl(21 90% 48%)",
    border: "hsl(21 90% 48% / 0.3)",
  },
  /** Unchanged - no changes */
  unchanged: {
    bg: "transparent",
    text: "hsl(var(--foreground))",
    border: "hsl(var(--border))",
  },
} as const;

/**
 * Surface colors for backgrounds and containers
 */
export const surfaceColors = {
  /** Main background */
  background: "hsl(var(--background))",
  /** Foreground text on background */
  foreground: "hsl(var(--foreground))",
  /** Card surfaces */
  card: {
    DEFAULT: "hsl(var(--card))",
    foreground: "hsl(var(--card-foreground))",
  },
  /** Popover surfaces */
  popover: {
    DEFAULT: "hsl(var(--popover))",
    foreground: "hsl(var(--popover-foreground))",
  },
  /** Muted surfaces for subtle backgrounds */
  muted: {
    DEFAULT: "hsl(var(--muted))",
    foreground: "hsl(var(--muted-foreground))",
  },
  /** Accent surfaces for highlights */
  accent: {
    DEFAULT: "hsl(var(--accent))",
    foreground: "hsl(var(--accent-foreground))",
  },
} as const;

/**
 * Elevation colors for dark mode depth
 * Used to create visual hierarchy with layered surfaces
 */
export const elevationColors = {
  /** Level 1 - Lowest elevation (e.g., sidebar) */
  1: "hsl(var(--slate-elevation-1))",
  /** Level 2 - Cards and panels */
  2: "hsl(var(--slate-elevation-2))",
  /** Level 3 - Dropdowns and popovers */
  3: "hsl(var(--slate-elevation-3))",
  /** Level 4 - Modals */
  4: "hsl(var(--slate-elevation-4))",
  /** Level 5 - Highest elevation (e.g., tooltips) */
  5: "hsl(var(--slate-elevation-5))",
} as const;

/**
 * Border and input colors
 */
export const borderColors = {
  /** Default border color */
  DEFAULT: "hsl(var(--border))",
  /** Input field borders */
  input: "hsl(var(--input))",
  /** Focus ring color */
  ring: "hsl(var(--ring))",
} as const;

/**
 * Combined colors export for easy access
 */
export const colors = {
  brand: brandColors,
  semantic: semanticColors,
  status: statusColors,
  comparison: comparisonColors,
  surface: surfaceColors,
  elevation: elevationColors,
  border: borderColors,
} as const;

export type Colors = typeof colors;
export type StatusColorKey = keyof typeof statusColors;
export type ComparisonColorKey = keyof typeof comparisonColors;
