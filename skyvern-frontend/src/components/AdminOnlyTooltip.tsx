import type { ReactNode } from "react";

import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { usePageSlots } from "@/store/PageSlots";

const ADMIN_ONLY_REASON =
  "Organization admin access is required for this action.";
const PERMISSIONS_LOADING = "Checking permissions…";

function AdminOnlyTooltip({
  allowed,
  children,
  className = "inline-flex",
}: {
  allowed: boolean;
  children: ReactNode;
  className?: string;
}) {
  const { permissions } = usePageSlots();

  if (allowed) return children;

  const message =
    permissions?.status === "ready"
      ? ADMIN_ONLY_REASON
      : permissions?.status === "loading"
        ? PERMISSIONS_LOADING
        : undefined;

  if (!message) return <div className={className}>{children}</div>;

  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <div
            tabIndex={0}
            role="group"
            aria-label={message}
            className={className}
          >
            {children}
          </div>
        </TooltipTrigger>
        <TooltipContent>{message}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

export { AdminOnlyTooltip };
