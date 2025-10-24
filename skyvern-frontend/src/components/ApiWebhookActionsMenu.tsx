import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { toast } from "@/components/ui/use-toast";
import { copyText } from "@/util/copyText";
import {
  generateApiCommands,
  type ApiCommandOptions,
} from "@/util/apiCommands";

type ApiWebhookActionsMenuProps = {
  getOptions: () => ApiCommandOptions;
  runId?: string;
  webhookDisabled?: boolean;
  onTestWebhook: () => void;
};

export function ApiWebhookActionsMenu({
  getOptions,
  webhookDisabled = false,
  onTestWebhook,
}: ApiWebhookActionsMenuProps) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button type="button" variant="secondary">
          API & Webhooks
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent>
        <DropdownMenuLabel className="py-2 text-base">
          Re-run via API
        </DropdownMenuLabel>
        <DropdownMenuItem
          onSelect={() => {
            const { curl } = generateApiCommands(getOptions());
            copyText(curl).then(() => {
              toast({
                variant: "success",
                title: "Copied to Clipboard",
                description:
                  "The cURL command has been copied to your clipboard.",
              });
            });
          }}
        >
          Copy cURL (Unix/Linux/macOS)
        </DropdownMenuItem>
        <DropdownMenuItem
          onSelect={() => {
            const { powershell } = generateApiCommands(getOptions());
            copyText(powershell).then(() => {
              toast({
                variant: "success",
                title: "Copied to Clipboard",
                description:
                  "The PowerShell command has been copied to your clipboard.",
              });
            });
          }}
        >
          Copy PowerShell (Windows)
        </DropdownMenuItem>
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
          Test Webhook
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
