/**
 * Skyvern Design System - Spacing Tokens
 *
 * Based on a 4px grid system for consistent spacing
 * throughout the application.
 */

/**
 * Base spacing scale
 * Values are in rem units (base: 16px)
 */
export const spacing = {
  /** 0px */
  0: "0",
  /** 1px - hairline */
  px: "1px",
  /** 2px - micro spacing */
  0.5: "0.125rem",
  /** 4px - tight spacing */
  1: "0.25rem",
  /** 6px */
  1.5: "0.375rem",
  /** 8px - compact spacing */
  2: "0.5rem",
  /** 10px */
  2.5: "0.625rem",
  /** 12px - default small */
  3: "0.75rem",
  /** 14px */
  3.5: "0.875rem",
  /** 16px - default medium */
  4: "1rem",
  /** 20px */
  5: "1.25rem",
  /** 24px - default large */
  6: "1.5rem",
  /** 28px */
  7: "1.75rem",
  /** 32px */
  8: "2rem",
  /** 36px */
  9: "2.25rem",
  /** 40px */
  10: "2.5rem",
  /** 44px */
  11: "2.75rem",
  /** 48px */
  12: "3rem",
  /** 56px */
  14: "3.5rem",
  /** 64px */
  16: "4rem",
  /** 80px */
  20: "5rem",
  /** 96px */
  24: "6rem",
  /** 112px */
  28: "7rem",
  /** 128px */
  32: "8rem",
  /** 144px */
  36: "9rem",
  /** 160px */
  40: "10rem",
  /** 176px */
  44: "11rem",
  /** 192px */
  48: "12rem",
  /** 208px */
  52: "13rem",
  /** 224px */
  56: "14rem",
  /** 240px */
  60: "15rem",
  /** 256px */
  64: "16rem",
  /** 288px */
  72: "18rem",
  /** 320px */
  80: "20rem",
  /** 384px */
  96: "24rem",
} as const;

/**
 * Semantic spacing for common UI patterns
 * Named for their intended use case
 */
export const semanticSpacing = {
  /** Inline spacing between icon and text */
  iconGap: spacing[2], // 8px
  /** Gap between form field and label */
  labelGap: spacing[1.5], // 6px
  /** Gap between form field and helper text */
  helperGap: spacing[1], // 4px
  /** Padding inside buttons */
  buttonPaddingX: spacing[4], // 16px
  buttonPaddingY: spacing[2], // 8px
  /** Padding inside cards */
  cardPadding: spacing[6], // 24px
  cardPaddingSm: spacing[4], // 16px
  /** Padding inside inputs */
  inputPaddingX: spacing[3], // 12px
  inputPaddingY: spacing[2], // 8px
  /** Gap between stacked form fields */
  formGap: spacing[4], // 16px
  /** Gap between sections */
  sectionGap: spacing[8], // 32px
  /** Page margin/padding */
  pageMargin: spacing[6], // 24px
  pageMarginLg: spacing[8], // 32px
  /** Sidebar width (collapsed/expanded) */
  sidebarCollapsed: spacing[16], // 64px
  sidebarExpanded: "240px",
} as const;

/**
 * Container max-widths
 */
export const containerWidths = {
  /** Extra small screens */
  xs: "20rem", // 320px
  /** Small screens */
  sm: "24rem", // 384px
  /** Medium screens */
  md: "28rem", // 448px
  /** Large screens */
  lg: "32rem", // 512px
  /** Extra large screens */
  xl: "36rem", // 576px
  /** 2XL screens */
  "2xl": "42rem", // 672px
  /** 3XL screens */
  "3xl": "48rem", // 768px
  /** 4XL screens */
  "4xl": "56rem", // 896px
  /** 5XL screens */
  "5xl": "64rem", // 1024px
  /** 6XL screens */
  "6xl": "72rem", // 1152px
  /** 7XL screens */
  "7xl": "80rem", // 1280px
  /** Full width */
  full: "100%",
  /** Prose/content width */
  prose: "65ch",
} as const;

/**
 * Z-index scale for layering
 */
export const zIndex = {
  /** Below default layer */
  behind: -1,
  /** Default layer */
  base: 0,
  /** Raised elements (cards, etc.) */
  raised: 10,
  /** Dropdown menus */
  dropdown: 50,
  /** Sticky elements */
  sticky: 100,
  /** Fixed elements (headers, etc.) */
  fixed: 200,
  /** Overlay backgrounds */
  overlay: 300,
  /** Modal dialogs */
  modal: 400,
  /** Popovers */
  popover: 500,
  /** Tooltips */
  tooltip: 600,
  /** Toast notifications */
  toast: 700,
  /** Highest priority (debugging, etc.) */
  max: 9999,
} as const;

export type Spacing = typeof spacing;
export type SpacingKey = keyof typeof spacing;
export type SemanticSpacing = typeof semanticSpacing;
export type ContainerWidth = keyof typeof containerWidths;
export type ZIndex = keyof typeof zIndex;
