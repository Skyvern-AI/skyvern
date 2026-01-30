/**
 * Skyvern Design System - Class Name Utility
 *
 * Combines clsx and tailwind-merge for intelligent
 * class name composition. Handles conditional classes
 * and prevents Tailwind class conflicts.
 */

import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

/**
 * Merge class names with Tailwind conflict resolution
 *
 * @example
 * // Basic usage
 * cn("px-2 py-1", "p-4") // => "p-4"
 *
 * @example
 * // Conditional classes
 * cn("base-class", isActive && "active-class", { "conditional": true })
 *
 * @example
 * // With component props
 * cn(buttonVariants({ variant, size }), className)
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
