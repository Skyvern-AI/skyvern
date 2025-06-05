import { CopyIcon } from "@radix-ui/react-icons";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/use-toast";
import { copyText } from "@/util/copyText";
import {
  type ApiRequest,
  getCurlCommand,
  getPowerShellCommand,
} from "@/util/apiCommands";

interface Props {
  getRequest: () => ApiRequest;
}

function CopyApiCommandDropdown({ getRequest }: Props) {
  const { toast } = useToast();

  const handleCopy = async (type: "curl" | "powershell") => {
    const request = getRequest();
    const text =
      type === "curl" ? getCurlCommand(request) : getPowerShellCommand(request);
    await copyText(text);
    toast({
      variant: "success",
      title: "Copied to Clipboard",
      description:
        type === "curl"
          ? "The cURL command has been copied to your clipboard."
          : "The PowerShell command has been copied to your clipboard.",
    });
  };

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="secondary">
          <CopyIcon className="mr-2 h-4 w-4" />
          Copy API Command
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuItem onSelect={() => handleCopy("curl")}>
          Copy cURL (Unix/Linux/macOS)
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => handleCopy("powershell")}>
          Copy PowerShell (Windows)
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export { CopyApiCommandDropdown };
