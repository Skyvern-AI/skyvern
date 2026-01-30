import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "../../utils/cn";

/**
 * Spinner variants using CVA
 */
const spinnerVariants = cva(
  "animate-spin rounded-full border-current border-t-transparent",
  {
    variants: {
      /**
       * Size variants
       */
      size: {
        /** Extra small - 12px */
        xs: "h-3 w-3 border",
        /** Small - 16px */
        sm: "h-4 w-4 border-2",
        /** Medium - 24px (default) */
        md: "h-6 w-6 border-2",
        /** Large - 32px */
        lg: "h-8 w-8 border-[3px]",
        /** Extra large - 48px */
        xl: "h-12 w-12 border-4",
      },
      /**
       * Color variants
       */
      variant: {
        /** Default - uses current text color */
        default: "text-foreground/30",
        /** Primary brand color */
        primary: "text-primary",
        /** Secondary color */
        secondary: "text-secondary-foreground/50",
        /** Muted color for subtle loading */
        muted: "text-muted-foreground/50",
        /** White - for use on dark backgrounds */
        white: "text-white/80",
      },
    },
    defaultVariants: {
      size: "md",
      variant: "default",
    },
  },
);

export interface SpinnerProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof spinnerVariants> {
  /**
   * Accessible label for screen readers
   * @default "Loading"
   */
  label?: string;
}

/**
 * Spinner Component
 *
 * A loading indicator that can be used inline or as a page-level loader.
 *
 * @example
 * // Basic usage
 * <Spinner />
 *
 * @example
 * // Different sizes
 * <Spinner size="sm" />
 * <Spinner size="lg" />
 *
 * @example
 * // With custom color
 * <Spinner variant="primary" />
 *
 * @example
 * // Inside a button
 * <Button disabled>
 *   <Spinner size="sm" variant="white" />
 *   Loading...
 * </Button>
 */
const Spinner = React.forwardRef<HTMLDivElement, SpinnerProps>(
  ({ className, size, variant, label = "Loading", ...props }, ref) => {
    return (
      <div
        ref={ref}
        role="status"
        aria-label={label}
        className={cn(spinnerVariants({ size, variant }), className)}
        {...props}
      >
        <span className="sr-only">{label}</span>
      </div>
    );
  },
);

Spinner.displayName = "Spinner";

export { Spinner, spinnerVariants };
