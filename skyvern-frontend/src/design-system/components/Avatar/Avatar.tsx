import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "../../utils/cn";

/**
 * Avatar container variants
 */
const avatarVariants = cva(
  "relative flex shrink-0 overflow-hidden rounded-full",
  {
    variants: {
      /**
       * Size variants
       */
      size: {
        /** Extra small - 24px */
        xs: "h-6 w-6 text-xs",
        /** Small - 32px */
        sm: "h-8 w-8 text-sm",
        /** Medium - 40px (default) */
        md: "h-10 w-10 text-base",
        /** Large - 48px */
        lg: "h-12 w-12 text-lg",
        /** Extra large - 64px */
        xl: "h-16 w-16 text-xl",
        /** 2XL - 80px */
        "2xl": "h-20 w-20 text-2xl",
      },
    },
    defaultVariants: {
      size: "md",
    },
  },
);

/**
 * Fallback background colors for initials
 * Cycles through these colors based on the name
 */
const fallbackColors = [
  "bg-red-500",
  "bg-orange-500",
  "bg-amber-500",
  "bg-yellow-500",
  "bg-lime-500",
  "bg-green-500",
  "bg-emerald-500",
  "bg-teal-500",
  "bg-cyan-500",
  "bg-sky-500",
  "bg-blue-500",
  "bg-indigo-500",
  "bg-violet-500",
  "bg-purple-500",
  "bg-fuchsia-500",
  "bg-pink-500",
  "bg-rose-500",
] as const;

/**
 * Get a consistent color based on a string (name)
 */
function getColorFromName(name: string): string {
  let hash = 0;
  for (let i = 0; i < name.length; i++) {
    const char = name.charCodeAt(i);
    hash = (hash << 5) - hash + char;
    hash = hash & hash; // Convert to 32bit integer
  }
  const index = Math.abs(hash) % fallbackColors.length;
  return fallbackColors[index];
}

/**
 * Get initials from a name
 */
function getInitials(name: string, maxLength: number = 2): string {
  const parts = name.trim().split(/\s+/);
  if (parts.length === 1) {
    return parts[0].substring(0, maxLength).toUpperCase();
  }
  return parts
    .slice(0, maxLength)
    .map((part) => part[0])
    .join("")
    .toUpperCase();
}

export interface AvatarProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof avatarVariants> {
  /**
   * Image source URL
   */
  src?: string | null;
  /**
   * Alt text for the image
   */
  alt?: string;
  /**
   * Name to use for fallback initials and color
   */
  name?: string;
  /**
   * Custom fallback content (overrides name-based initials)
   */
  fallback?: React.ReactNode;
}

/**
 * Avatar Component
 *
 * Displays a user avatar with image, initials fallback, or custom content.
 *
 * @example
 * // With image
 * <Avatar src="/user.jpg" alt="John Doe" name="John Doe" />
 *
 * @example
 * // Initials fallback (when src fails or not provided)
 * <Avatar name="John Doe" />
 *
 * @example
 * // Custom fallback
 * <Avatar fallback={<UserIcon />} />
 *
 * @example
 * // Different sizes
 * <Avatar size="sm" name="Jane" />
 * <Avatar size="lg" name="Jane" />
 */
const Avatar = React.forwardRef<HTMLDivElement, AvatarProps>(
  ({ className, size, src, alt, name, fallback, ...props }, ref) => {
    const [imageError, setImageError] = React.useState(false);
    const [imageLoaded, setImageLoaded] = React.useState(false);

    // Reset state when src changes
    React.useEffect(() => {
      setImageError(false);
      setImageLoaded(false);
    }, [src]);

    const showImage = src && !imageError;
    const showFallback = !showImage || !imageLoaded;
    const initials = name ? getInitials(name) : "";
    const bgColor = name ? getColorFromName(name) : "bg-muted";

    return (
      <div
        ref={ref}
        className={cn(avatarVariants({ size }), className)}
        {...props}
      >
        {/* Image */}
        {showImage && (
          <img
            src={src}
            alt={alt || name || "Avatar"}
            className={cn(
              "aspect-square h-full w-full object-cover",
              !imageLoaded && "invisible",
            )}
            onLoad={() => setImageLoaded(true)}
            onError={() => setImageError(true)}
          />
        )}

        {/* Fallback */}
        {showFallback && (
          <div
            className={cn(
              "flex h-full w-full items-center justify-center font-medium text-white",
              bgColor,
            )}
            aria-hidden={showImage && imageLoaded}
          >
            {fallback ?? initials ?? (
              <svg
                className="h-1/2 w-1/2 text-current opacity-70"
                fill="currentColor"
                viewBox="0 0 24 24"
              >
                <path d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z" />
              </svg>
            )}
          </div>
        )}
      </div>
    );
  },
);

Avatar.displayName = "Avatar";

/**
 * Avatar Group Component
 *
 * Displays multiple avatars in a stacked layout.
 *
 * @example
 * <AvatarGroup max={3}>
 *   <Avatar name="Alice" />
 *   <Avatar name="Bob" />
 *   <Avatar name="Charlie" />
 *   <Avatar name="Diana" />
 * </AvatarGroup>
 */
export interface AvatarGroupProps extends React.HTMLAttributes<HTMLDivElement> {
  /**
   * Maximum number of avatars to show before "+N" indicator
   */
  max?: number;
  /**
   * Size of avatars in the group
   */
  size?: VariantProps<typeof avatarVariants>["size"];
}

const AvatarGroup = React.forwardRef<HTMLDivElement, AvatarGroupProps>(
  ({ className, children, max = 4, size = "md", ...props }, ref) => {
    const childArray = React.Children.toArray(children);
    const visibleChildren = max ? childArray.slice(0, max) : childArray;
    const remainingCount = Math.max(0, childArray.length - (max || 0));

    return (
      <div
        ref={ref}
        className={cn("flex -space-x-2", className)}
        {...props}
      >
        {visibleChildren.map((child, index) => (
          <div
            key={index}
            className="ring-2 ring-background rounded-full"
          >
            {React.isValidElement(child)
              ? React.cloneElement(child as React.ReactElement<AvatarProps>, { size })
              : child}
          </div>
        ))}
        {remainingCount > 0 && (
          <div
            className={cn(
              avatarVariants({ size }),
              "flex items-center justify-center bg-muted text-muted-foreground font-medium ring-2 ring-background",
            )}
          >
            +{remainingCount}
          </div>
        )}
      </div>
    );
  },
);

AvatarGroup.displayName = "AvatarGroup";

export { Avatar, AvatarGroup, avatarVariants, getInitials, getColorFromName };
