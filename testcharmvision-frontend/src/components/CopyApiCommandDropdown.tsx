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
          Copy API Command
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent>
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
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export { CopyApiCommandDropdown };
