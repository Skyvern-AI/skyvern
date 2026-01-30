import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "../../utils/cn";

/**
 * Status values matching the API Status enum
 * from src/api/types.ts
 */
export const StatusValue = {
  Created: "created",
  Running: "running",
  Failed: "failed",
  Terminated: "terminated",
  Completed: "completed",
  Queued: "queued",
  TimedOut: "timed_out",
  Canceled: "canceled",
  Skipped: "skipped",
  Paused: "paused",
} as const;

export type StatusValue = (typeof StatusValue)[keyof typeof StatusValue];

/**
 * Status configuration mapping
 */
const statusConfig: Record<
  StatusValue,
  {
    label: string;
    variant: "default" | "info" | "success" | "warning" | "destructive";
    showDot: boolean;
    dotPulse: boolean;
  }
> = {
  created: {
    label: "Created",
    variant: "default",
    showDot: false,
    dotPulse: false,
  },
  running: {
    label: "Running",
    variant: "info",
    showDot: true,
    dotPulse: true,
  },
  queued: {
    label: "Queued",
    variant: "warning",
    showDot: true,
    dotPulse: true,
  },
  completed: {
    label: "Completed",
    variant: "success",
    showDot: false,
    dotPulse: false,
  },
  failed: {
    label: "Failed",
    variant: "destructive",
    showDot: false,
    dotPulse: false,
  },
  terminated: {
    label: "Terminated",
    variant: "destructive",
    showDot: false,
    dotPulse: false,
  },
  canceled: {
    label: "Canceled",
    variant: "default",
    showDot: false,
    dotPulse: false,
  },
  timed_out: {
    label: "Timed Out",
    variant: "warning",
    showDot: false,
    dotPulse: false,
  },
  skipped: {
    label: "Skipped",
    variant: "default",
    showDot: false,
    dotPulse: false,
  },
  paused: {
    label: "Paused",
    variant: "warning",
    showDot: true,
    dotPulse: false,
  },
};

/**
 * StatusBadge variants
 */
const statusBadgeVariants = cva(
  "inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium transition-colors",
  {
    variants: {
      variant: {
        default: "bg-muted text-muted-foreground",
        info: "bg-info/10 text-info border border-info/20",
        success: "bg-success/10 text-success border border-success/20",
        warning: "bg-warning/10 text-warning border border-warning/20",
        destructive:
          "bg-destructive/10 text-destructive border border-destructive/20",
      },
      size: {
        sm: "px-2 py-0.5 text-[10px]",
        md: "px-2.5 py-0.5 text-xs",
        lg: "px-3 py-1 text-sm",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "md",
    },
  },
);

/**
 * Dot indicator variants
 */
const dotVariants = cva("rounded-full", {
  variants: {
    variant: {
      default: "bg-muted-foreground",
      info: "bg-info",
      success: "bg-success",
      warning: "bg-warning",
      destructive: "bg-destructive",
    },
    size: {
      sm: "h-1 w-1",
      md: "h-1.5 w-1.5",
      lg: "h-2 w-2",
    },
    pulse: {
      true: "animate-pulse",
      false: "",
    },
  },
  defaultVariants: {
    variant: "default",
    size: "md",
    pulse: false,
  },
});

export interface StatusBadgeProps
  extends Omit<React.HTMLAttributes<HTMLSpanElement>, "children">,
    VariantProps<typeof statusBadgeVariants> {
  /**
   * The status value to display
   */
  status: StatusValue;
  /**
   * Override the default label
   */
  label?: string;
  /**
   * Show/hide the status dot indicator
   */
  showDot?: boolean;
  /**
   * Show icon instead of or with dot
   */
  icon?: React.ReactNode;
}

/**
 * StatusBadge Component
 *
 * Displays a status indicator for tasks, workflows, and runs.
 * Automatically maps status values to appropriate colors and labels.
 *
 * @example
 * // Basic usage with status value
 * <StatusBadge status="running" />
 * <StatusBadge status="completed" />
 * <StatusBadge status="failed" />
 *
 * @example
 * // With custom label
 * <StatusBadge status="running" label="In Progress" />
 *
 * @example
 * // Different sizes
 * <StatusBadge status="queued" size="sm" />
 * <StatusBadge status="queued" size="lg" />
 *
 * @example
 * // With custom icon
 * <StatusBadge status="completed" icon={<CheckIcon />} />
 */
const StatusBadge = React.forwardRef<HTMLSpanElement, StatusBadgeProps>(
  (
    { className, status, label, size, showDot: showDotProp, icon, ...props },
    ref,
  ) => {
    const config = statusConfig[status];
    const displayLabel = label ?? config.label;
    const showDot = showDotProp ?? config.showDot;

    return (
      <span
        ref={ref}
        className={cn(
          statusBadgeVariants({ variant: config.variant, size }),
          className,
        )}
        {...props}
      >
        {icon}
        {!icon && showDot && (
          <span
            className={cn(
              dotVariants({
                variant: config.variant,
                size,
                pulse: config.dotPulse,
              }),
            )}
            aria-hidden="true"
          />
        )}
        {displayLabel}
      </span>
    );
  },
);

StatusBadge.displayName = "StatusBadge";

/**
 * StatusDot Component
 *
 * A standalone status dot indicator without text.
 *
 * @example
 * <StatusDot status="running" />
 */
export interface StatusDotProps
  extends React.HTMLAttributes<HTMLSpanElement> {
  status: StatusValue;
  size?: "sm" | "md" | "lg";
}

const StatusDot = React.forwardRef<HTMLSpanElement, StatusDotProps>(
  ({ className, status, size = "md", ...props }, ref) => {
    const config = statusConfig[status];

    return (
      <span
        ref={ref}
        role="status"
        aria-label={config.label}
        className={cn(
          dotVariants({
            variant: config.variant,
            size,
            pulse: config.dotPulse,
          }),
          className,
        )}
        {...props}
      />
    );
  },
);

StatusDot.displayName = "StatusDot";

/**
 * Get status configuration for custom implementations
 */
function getStatusConfig(status: StatusValue) {
  return statusConfig[status];
}

/**
 * Check if a status is in a "live" state (running or queued)
 */
function isLiveStatus(status: StatusValue): boolean {
  return status === "running" || status === "queued" || status === "paused";
}

/**
 * Check if a status is in a terminal state
 */
function isTerminalStatus(status: StatusValue): boolean {
  return (
    status === "completed" ||
    status === "failed" ||
    status === "terminated" ||
    status === "canceled" ||
    status === "timed_out" ||
    status === "skipped"
  );
}

export {
  StatusBadge,
  StatusDot,
  statusBadgeVariants,
  dotVariants,
  statusConfig,
  getStatusConfig,
  isLiveStatus,
  isTerminalStatus,
};
