/**
 * Skyvern Design System - Animation Tokens
 *
 * Duration, easing, and animation presets for
 * consistent motion throughout the application.
 */

/**
 * Animation durations
 */
export const duration = {
  /** Instant - no animation */
  instant: "0ms",
  /** Fastest - micro interactions */
  fastest: "50ms",
  /** Fast - quick feedback */
  fast: "100ms",
  /** Normal - standard transitions */
  normal: "150ms",
  /** Moderate - emphasized transitions */
  moderate: "200ms",
  /** Slow - deliberate animations */
  slow: "300ms",
  /** Slower - major transitions */
  slower: "400ms",
  /** Slowest - dramatic effects */
  slowest: "500ms",
} as const;

/**
 * Easing functions
 * Based on common motion curves for natural-feeling animations
 */
export const easing = {
  /** Default easing - smooth acceleration and deceleration */
  DEFAULT: "cubic-bezier(0.4, 0, 0.2, 1)",
  /** Linear - constant speed */
  linear: "linear",
  /** Ease in - starts slow, ends fast */
  in: "cubic-bezier(0.4, 0, 1, 1)",
  /** Ease out - starts fast, ends slow (most common for UI) */
  out: "cubic-bezier(0, 0, 0.2, 1)",
  /** Ease in-out - slow start and end */
  inOut: "cubic-bezier(0.4, 0, 0.2, 1)",
  /** Bounce - overshoots then settles */
  bounce: "cubic-bezier(0.68, -0.55, 0.265, 1.55)",
  /** Elastic - spring-like effect */
  elastic: "cubic-bezier(0.68, -0.6, 0.32, 1.6)",
  /** Smooth - very gentle curve */
  smooth: "cubic-bezier(0.25, 0.1, 0.25, 1)",
} as const;

/**
 * Pre-defined transitions for common UI patterns
 */
export const transitions = {
  /** Default transition for most UI elements */
  DEFAULT: `all ${duration.normal} ${easing.DEFAULT}`,
  /** Fast transition for hover states */
  fast: `all ${duration.fast} ${easing.out}`,
  /** Slow transition for major changes */
  slow: `all ${duration.slow} ${easing.inOut}`,
  /** Color transition only */
  colors: `color ${duration.normal} ${easing.DEFAULT}, background-color ${duration.normal} ${easing.DEFAULT}, border-color ${duration.normal} ${easing.DEFAULT}`,
  /** Opacity transition */
  opacity: `opacity ${duration.normal} ${easing.DEFAULT}`,
  /** Transform transition */
  transform: `transform ${duration.normal} ${easing.DEFAULT}`,
  /** Shadow transition */
  shadow: `box-shadow ${duration.normal} ${easing.DEFAULT}`,
  /** None - disable transitions */
  none: "none",
} as const;

/**
 * Keyframe animations
 * These can be used with CSS @keyframes
 */
export const keyframes = {
  /** Fade in from transparent */
  fadeIn: {
    from: { opacity: "0" },
    to: { opacity: "1" },
  },
  /** Fade out to transparent */
  fadeOut: {
    from: { opacity: "1" },
    to: { opacity: "0" },
  },
  /** Scale up from smaller */
  scaleIn: {
    from: { transform: "scale(0.95)", opacity: "0" },
    to: { transform: "scale(1)", opacity: "1" },
  },
  /** Scale down to smaller */
  scaleOut: {
    from: { transform: "scale(1)", opacity: "1" },
    to: { transform: "scale(0.95)", opacity: "0" },
  },
  /** Slide in from top */
  slideInFromTop: {
    from: { transform: "translateY(-10px)", opacity: "0" },
    to: { transform: "translateY(0)", opacity: "1" },
  },
  /** Slide in from bottom */
  slideInFromBottom: {
    from: { transform: "translateY(10px)", opacity: "0" },
    to: { transform: "translateY(0)", opacity: "1" },
  },
  /** Slide in from left */
  slideInFromLeft: {
    from: { transform: "translateX(-10px)", opacity: "0" },
    to: { transform: "translateX(0)", opacity: "1" },
  },
  /** Slide in from right */
  slideInFromRight: {
    from: { transform: "translateX(10px)", opacity: "0" },
    to: { transform: "translateX(0)", opacity: "1" },
  },
  /** Spin animation for loading indicators */
  spin: {
    from: { transform: "rotate(0deg)" },
    to: { transform: "rotate(360deg)" },
  },
  /** Pulse animation for attention */
  pulse: {
    "0%, 100%": { opacity: "1" },
    "50%": { opacity: "0.5" },
  },
  /** Bounce animation */
  bounce: {
    "0%, 100%": { transform: "translateY(-25%)", animationTimingFunction: easing.bounce },
    "50%": { transform: "translateY(0)", animationTimingFunction: easing.bounce },
  },
  /** Shake animation for errors */
  shake: {
    "0%, 100%": { transform: "translateX(0)" },
    "10%, 30%, 50%, 70%, 90%": { transform: "translateX(-4px)" },
    "20%, 40%, 60%, 80%": { transform: "translateX(4px)" },
  },
  /** Accordion expand */
  accordionDown: {
    from: { height: "0" },
    to: { height: "var(--radix-accordion-content-height)" },
  },
  /** Accordion collapse */
  accordionUp: {
    from: { height: "var(--radix-accordion-content-height)" },
    to: { height: "0" },
  },
} as const;

/**
 * Named animation presets combining duration and easing
 */
export const animationPresets = {
  /** Fade in animation */
  fadeIn: `fadeIn ${duration.normal} ${easing.out}`,
  /** Fade out animation */
  fadeOut: `fadeOut ${duration.normal} ${easing.in}`,
  /** Scale in animation */
  scaleIn: `scaleIn ${duration.normal} ${easing.out}`,
  /** Scale out animation */
  scaleOut: `scaleOut ${duration.fast} ${easing.in}`,
  /** Slide in from top */
  slideInTop: `slideInFromTop ${duration.moderate} ${easing.out}`,
  /** Slide in from bottom */
  slideInBottom: `slideInFromBottom ${duration.moderate} ${easing.out}`,
  /** Slide in from left */
  slideInLeft: `slideInFromLeft ${duration.moderate} ${easing.out}`,
  /** Slide in from right */
  slideInRight: `slideInFromRight ${duration.moderate} ${easing.out}`,
  /** Spinner animation */
  spin: `spin 1s ${easing.linear} infinite`,
  /** Pulse animation */
  pulse: `pulse 2s ${easing.inOut} infinite`,
  /** Bounce animation */
  bounce: `bounce 1s infinite`,
  /** Accordion expand */
  accordionDown: `accordionDown ${duration.moderate} ${easing.out}`,
  /** Accordion collapse */
  accordionUp: `accordionUp ${duration.moderate} ${easing.out}`,
} as const;

/**
 * Combined animations export
 */
export const animations = {
  duration,
  easing,
  transitions,
  keyframes,
  presets: animationPresets,
} as const;

export type Duration = keyof typeof duration;
export type Easing = keyof typeof easing;
export type Transition = keyof typeof transitions;
export type AnimationPreset = keyof typeof animationPresets;
