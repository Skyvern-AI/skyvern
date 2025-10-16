import { Button } from "@/components/ui/button";
import { CardStackIcon, PlusIcon } from "@radix-ui/react-icons";
import {
  CredentialModalTypes,
  useCredentialModalState,
} from "./useCredentialModalState";
import { CredentialsModal } from "./CredentialsModal";
import { CredentialsList } from "./CredentialsList";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { KeyIcon } from "@/components/icons/KeyIcon";

const subHeaderText =
  "Securely store your passwords or credit cards here to link them throughout your workflows.";

function CredentialsPage() {
  const { setIsOpen, setType } = useCredentialModalState();
  return (
    <div className="space-y-5">
      <h1 className="text-2xl">Credentials</h1>
      <div className="flex items-center justify-between">
        <div className="w-96 text-sm text-slate-300">{subHeaderText}</div>
        <DropdownMenu modal={false}>
          <DropdownMenuTrigger asChild>
            <Button>
              <PlusIcon className="mr-2 size-6" /> Add
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent className="w-48">
            <DropdownMenuItem
              onSelect={() => {
                setIsOpen(true);
                setType(CredentialModalTypes.PASSWORD);
              }}
              className="cursor-pointer"
            >
              <KeyIcon className="mr-2 size-4" />
              Password
            </DropdownMenuItem>
            <DropdownMenuItem
              onSelect={() => {
                setIsOpen(true);
                setType(CredentialModalTypes.CREDIT_CARD);
              }}
              className="cursor-pointer"
            >
              <CardStackIcon className="mr-2 size-4" />
              Credit Card
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      <CredentialsList />
      <CredentialsModal />

      {/* Footer note */}
      <div className="mt-8 border-t border-slate-700 pt-4">
        <div className="text-sm italic text-slate-400">
          <strong>Note:</strong> This feature requires a Bitwarden-compatible
          server ({" "}
          <a
            href="https://bitwarden.com/help/self-host-an-organization/"
            target="_blank"
            rel="noopener noreferrer"
            className="text-blue-400 underline hover:text-blue-300"
          >
            self-hosted Bitwarden
          </a>{" "}
          ) or{" "}
          <a
            href="https://github.com/dani-garcia/vaultwarden"
            target="_blank"
            rel="noopener noreferrer"
            className="text-blue-400 underline hover:text-blue-300"
          >
            this community version
          </a>{" "}
          or a paid Bitwarden account. Make sure the relevant
          `SKYVERN_AUTH_BITWARDEN_*` environment variables are configured. See
          details{" "}
          <a
            href="https://docs.skyvern.com/credentials/bitwarden"
            target="_blank"
            rel="noopener noreferrer"
            className="text-blue-400 underline hover:text-blue-300"
          >
            here
          </a>
          .
        </div>
      </div>
    </div>
  );
}

export { CredentialsPage };
