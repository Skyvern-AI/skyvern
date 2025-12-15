import { CopyIcon } from "@radix-ui/react-icons";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Button } from "@/components/ui/button";
import { toast } from "@/components/ui/use-toast";
import { copyText } from "@/util/copyText";
import {
  generateApiCommands,
  type ApiCommandOptions,
} from "@/util/apiCommands";

interface Props {
  getOptions: () => ApiCommandOptions;
}

function CopyApiCommandDropdown({ getOptions }: Props) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button type="button" variant="secondary">
          <CopyIcon className="mr-2 h-4 w-4" />
          API 명령어 복사
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent>
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
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export { CopyApiCommandDropdown };
