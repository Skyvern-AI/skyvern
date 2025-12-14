import { useEffect } from "react";
import { Button } from "@/components/ui/button";
import { CardStackIcon, LockClosedIcon, PlusIcon } from "@radix-ui/react-icons";
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
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { CredentialsTotpTab } from "./CredentialsTotpTab";
import { useSearchParams } from "react-router-dom";

const subHeaderText =
  "Securely store your passwords, credit cards, secrets, and manage incoming 2FA codes for your workflows.";

const TAB_VALUES = [
  "passwords",
  "creditCards",
  "secrets",
  "twoFactor",
] as const;
type TabValue = (typeof TAB_VALUES)[number];
const DEFAULT_TAB: TabValue = "passwords";

function CredentialsPage() {
  const { setIsOpen, setType } = useCredentialModalState();
  const [searchParams, setSearchParams] = useSearchParams();
  const tabParam = searchParams.get("tab");
  const matchedTab = TAB_VALUES.find((tab) => tab === tabParam);
  const activeTab: TabValue = matchedTab ?? DEFAULT_TAB;

  useEffect(() => {
    if (tabParam && !matchedTab) {
      const params = new URLSearchParams(searchParams);
      params.set("tab", DEFAULT_TAB);
      setSearchParams(params, { replace: true });
    }
  }, [tabParam, matchedTab, searchParams, setSearchParams]);

  function handleTabChange(value: string) {
    const nextTab = TAB_VALUES.find((tab) => tab === value) ?? DEFAULT_TAB;
    const params = new URLSearchParams(searchParams);
    params.set("tab", nextTab);
    setSearchParams(params, { replace: true });
  }

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
            <DropdownMenuItem
              onSelect={() => {
                setIsOpen(true);
                setType(CredentialModalTypes.SECRET);
              }}
              className="cursor-pointer"
            >
              <LockClosedIcon className="mr-2 size-4" />
              Secret
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      <Tabs
        value={activeTab}
        className="space-y-4"
        onValueChange={handleTabChange}
      >
        <TabsList className="bg-slate-elevation1">
          <TabsTrigger value="passwords">Passwords</TabsTrigger>
          <TabsTrigger value="creditCards">Credit Cards</TabsTrigger>
          <TabsTrigger value="secrets">Secrets</TabsTrigger>
          <TabsTrigger value="twoFactor">2FA</TabsTrigger>
        </TabsList>

        <TabsContent value="passwords" className="space-y-4">
          <CredentialsList filter="password" />
        </TabsContent>

        <TabsContent value="creditCards" className="space-y-4">
          <CredentialsList filter="credit_card" />
        </TabsContent>

        <TabsContent value="secrets" className="space-y-4">
          <CredentialsList filter="secret" />
        </TabsContent>

        <TabsContent value="twoFactor" className="space-y-4">
          <CredentialsTotpTab />
        </TabsContent>
      </Tabs>
      <CredentialsModal />

      {/* Footer note - only for Passwords and Credit Cards tabs */}
      {activeTab !== "twoFactor" && (
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
      )}
    </div>
  );
}

export { CredentialsPage };
