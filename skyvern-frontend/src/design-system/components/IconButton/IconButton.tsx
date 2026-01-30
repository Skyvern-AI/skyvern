import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "../../utils/cn";

/**
 * IconButton variants
 */
const iconButtonVariants = cva(
  "inline-flex items-center justify-center rounded-md transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50",
  {
    variants: {
      variant: {
        default:
          "bg-primary text-primary-foreground shadow hover:bg-primary/90",
        secondary:
          "bg-secondary text-secondary-foreground shadow-sm hover:bg-secondary/80",
        outline:
          "border border-input bg-background shadow-sm hover:bg-accent hover:text-accent-foreground",
        ghost: "hover:bg-accent hover:text-accent-foreground",
        destructive:
          "bg-destructive text-destructive-foreground shadow-sm hover:bg-destructive/90",
        link: "text-primary underline-offset-4 hover:underline",
      },
      size: {
        xs: "h-6 w-6",
        sm: "h-8 w-8",
        md: "h-9 w-9",
        lg: "h-10 w-10",
        xl: "h-12 w-12",
      },
    },
    defaultVariants: {
      variant: "ghost",
      size: "md",
    },
  },
);

/**
 * Icon size mapping based on button size
 */
const iconSizeMap: Record<string, string> = {
  xs: "h-3 w-3",
  sm: "h-4 w-4",
  md: "h-4 w-4",
  lg: "h-5 w-5",
  xl: "h-6 w-6",
};

export interface IconButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof iconButtonVariants> {
  /**
   * Accessible label for the button (required for accessibility)
   */
  "aria-label": string;
  /**
   * The icon to display (should be a React element)
   */
  icon: React.ReactElement;
  /**
   * Show loading spinner instead of icon
   */
  isLoading?: boolean;
}

/**
 * IconButton Component
 *
 * A button that only displays an icon. Requires aria-label for accessibility.
 *
 * @example
 * // Basic usage
 * <IconButton icon={<PlusIcon />} aria-label="Add item" />
 *
 * @example
 * // Different variants
 * <IconButton icon={<TrashIcon />} aria-label="Delete" variant="destructive" />
 * <IconButton icon={<EditIcon />} aria-label="Edit" variant="outline" />
 *
 * @example
 * // Different sizes
 * <IconButton icon={<SearchIcon />} aria-label="Search" size="sm" />
 * <IconButton icon={<SearchIcon />} aria-label="Search" size="lg" />
 *
 * @example
 * // Loading state
 * <IconButton icon={<SaveIcon />} aria-label="Save" isLoading />
 */
const IconButton = React.forwardRef<HTMLButtonElement, IconButtonProps>(
  (
    {
      className,
      variant,
      size = "md",
      icon,
      isLoading,
      disabled,
      ...props
    },
    ref,
  ) => {
    const iconClassName = iconSizeMap[size || "md"];

    return (
      <button
        ref={ref}
        className={cn(iconButtonVariants({ variant, size }), className)}
        disabled={disabled || isLoading}
        {...props}
      >
        {isLoading ? (
          <svg
            className={cn("animate-spin", iconClassName)}
            xmlns="http://www.w3.org/2000/svg"
            fill="none"
            viewBox="0 0 24 24"
          >
            <circle
              className="opacity-25"
              cx="12"
              cy="12"
              r="10"
              stroke="currentColor"
              strokeWidth="4"
            />
            <path
              className="opacity-75"
              fill="currentColor"
              d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"
            />
          </svg>
        ) : (
          React.cloneElement(icon, {
            className: cn(iconClassName, icon.props.className),
            "aria-hidden": true,
          })
        )}
      </button>
    );
  },
);

IconButton.displayName = "IconButton";

export { IconButton, iconButtonVariants };
