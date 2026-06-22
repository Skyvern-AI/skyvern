import { ExclamationTriangleIcon } from "@radix-ui/react-icons";

// Single source of truth for how a "terminated" outcome looks across the app
// (run timeline, action cards, and the run-history status badge). Swap
// TerminatedIcon / the tone classes here to change it everywhere.
export const TerminatedIcon = ExclamationTriangleIcon;
export const terminatedTone = "text-amber-500";
export const terminatedBorder = "border-l-amber-500";
export const terminatedDot = "bg-amber-500";
