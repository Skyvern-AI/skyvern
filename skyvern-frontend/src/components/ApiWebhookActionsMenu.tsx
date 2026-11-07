import { type ReactElement, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { CopyApiCommandMenuItems } from "@/components/CopyApiCommandDropdown";
import { type ApiCommandOptions } from "@/util/apiCommands";

type ApiWebhookActionsMenuProps = {
  getOptions: () => ApiCommandOptions;
  runId?: string;
  webhookDisabled?: boolean;
  disabledReason?: string;
  onTestWebhook: () => void;
  size?: "default" | "sm";
  // Single element forwarded to the trigger via Radix `asChild`; when set,
  // `size` is unused. Defaults to the solid secondary button (legacy pages).
  trigger?: ReactElement;
  // Radix tooltip on the trigger; the host must provide a TooltipProvider.
  triggerTooltip?: ReactNode;
};

export function ApiWebhookActionsMenu({
  getOptions,
  webhookDisabled = false,
  disabledReason,
  onTestWebhook,
  size = "default",
  trigger,
  triggerTooltip,
}: ApiWebhookActionsMenuProps) {
  const menuTrigger = (
    <DropdownMenuTrigger asChild>
      {trigger ?? (
        <Button type="button" variant="secondary" size={size}>
          API & Webhooks
        </Button>
      )}
    </DropdownMenuTrigger>
  );
  return (
    <DropdownMenu>
      {triggerTooltip ? (
        <Tooltip>
          <TooltipTrigger asChild>{menuTrigger}</TooltipTrigger>
          <TooltipContent side="bottom">{triggerTooltip}</TooltipContent>
        </Tooltip>
      ) : (
        menuTrigger
      )}
      <DropdownMenuContent>
        <DropdownMenuLabel className="py-2 text-base">
          Re-run via API
        </DropdownMenuLabel>
        <CopyApiCommandMenuItems getOptions={getOptions} />
        <DropdownMenuSeparator />
        <DropdownMenuLabel className="py-2 text-base">
          Webhooks
        </DropdownMenuLabel>
        <DropdownMenuItem
          disabled={webhookDisabled}
          onSelect={() => {
            setTimeout(() => onTestWebhook(), 0);
          }}
        >
          <span>Test Webhook</span>
          {webhookDisabled && disabledReason ? (
            <span className="ml-2 text-xs text-muted-foreground">
              {disabledReason}
            </span>
          ) : null}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
