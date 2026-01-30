/**
 * Skyvern Design System - Components Barrel Export
 *
 * Central export point for all design system components.
 */

// Spinner
export {
  Spinner,
  spinnerVariants,
  type SpinnerProps,
} from "./Spinner";

// Avatar
export {
  Avatar,
  AvatarGroup,
  avatarVariants,
  getInitials,
  getColorFromName,
  type AvatarProps,
  type AvatarGroupProps,
} from "./Avatar";

// StatusBadge
export {
  StatusBadge,
  StatusDot,
  StatusValue,
  statusBadgeVariants,
  dotVariants,
  statusConfig,
  getStatusConfig,
  isLiveStatus,
  isTerminalStatus,
  type StatusBadgeProps,
  type StatusDotProps,
} from "./StatusBadge";

// IconButton
export {
  IconButton,
  iconButtonVariants,
  type IconButtonProps,
} from "./IconButton";
