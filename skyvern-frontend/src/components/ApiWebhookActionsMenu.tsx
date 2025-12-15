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
          API & 웹훅
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent>
        <DropdownMenuLabel className="py-2 text-base">
          API로 재실행
        </DropdownMenuLabel>
        <DropdownMenuItem
          onSelect={() => {
            const { curl } = generateApiCommands(getOptions());
            copyText(curl).then(() => {
              toast({
                variant: "success",
                title: "클립보드에 복사됨",
                description: "cURL 명령어가 클립보드에 복사되었습니다.",
              });
            });
          }}
        >
          cURL 복사 (Unix/Linux/macOS)
        </DropdownMenuItem>
        <DropdownMenuItem
          onSelect={() => {
            const { powershell } = generateApiCommands(getOptions());
            copyText(powershell).then(() => {
              toast({
                variant: "success",
                title: "클립보드에 복사됨",
                description:
                  "PowerShell 명령어가 클립보드에 복사되었습니다.",
              });
            });
          }}
        >
          PowerShell 복사 (Windows)
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuLabel className="py-2 text-base">
          웹훅
        </DropdownMenuLabel>
        <DropdownMenuItem
          disabled={webhookDisabled}
          onSelect={() => {
            setTimeout(() => onTestWebhook(), 0);
          }}
        >
          웹훅 테스트
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
