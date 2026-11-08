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
  describeCopiedCommand,
  type ApiCommandOptions,
} from "@/util/apiCommands";

interface Props {
  getOptions: () => ApiCommandOptions;
}

function CopyApiCommandMenuItems({ getOptions }: Props) {
  async function copyCommand(kind: "cURL" | "PowerShell") {
    const { curl, powershell } = generateApiCommands(getOptions());
    const copied = await copyText(kind === "cURL" ? curl : powershell);
    toast(
      copied
        ? {
            variant: "success",
            title: "Copied to Clipboard",
            description: describeCopiedCommand(kind),
          }
        : {
            variant: "destructive",
            title: "Copy failed",
            description: `Could not copy the ${kind} command. Check your browser's clipboard permissions.`,
          },
    );
  }

  return (
    <>
      <DropdownMenuItem onSelect={() => void copyCommand("cURL")}>
        Copy cURL (Unix/Linux/macOS)
      </DropdownMenuItem>
      <DropdownMenuItem onSelect={() => void copyCommand("PowerShell")}>
        Copy PowerShell (Windows)
      </DropdownMenuItem>
    </>
  );
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
        <CopyApiCommandMenuItems getOptions={getOptions} />
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export { CopyApiCommandDropdown, CopyApiCommandMenuItems };
