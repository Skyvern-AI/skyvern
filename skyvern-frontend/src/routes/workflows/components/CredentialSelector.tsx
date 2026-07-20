import type { CredentialApiResponse } from "@/api/types";
import { getHostname } from "@/util/getHostname";
import { CredentialsModal } from "@/routes/credentials/CredentialsModal";
import {
  CredentialModalTypes,
  useCredentialModalState,
} from "@/routes/credentials/useCredentialModalState";
import { CredentialCombobox } from "./CredentialCombobox";

type Props = {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  required?: boolean;
};

function CredentialSelector({ value, onChange, placeholder, required }: Props) {
  const { setIsOpen, setType } = useCredentialModalState();

  const renderCredentialItem = (credential: CredentialApiResponse) => (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <p className="text-sm font-medium">{credential.name}</p>
        {credential.browser_profile_id ? (
          <>
            <span className="rounded bg-green-100 px-1.5 py-0.5 text-[10px] text-green-700 dark:bg-green-900/40 dark:text-green-400">
              saved-profile
            </span>
            {credential.tested_url ? (
              <span className="text-[10px] text-muted-foreground">
                {getHostname(credential.tested_url)}
              </span>
            ) : null}
          </>
        ) : null}
      </div>
      <p className="text-xs text-muted-foreground">
        {credential.credential_type === "password"
          ? "Password"
          : credential.credential_type === "credit_card"
            ? "Credit Card"
            : "Secret"}
      </p>
    </div>
  );

  return (
    <>
      <CredentialCombobox
        value={value}
        selectedCredentialId={value || undefined}
        onValueChange={(nextValue) => onChange(nextValue)}
        onAddNew={() => {
          setIsOpen(true);
          setType(CredentialModalTypes.PASSWORD);
        }}
        renderCredentialItem={renderCredentialItem}
        placeholder={placeholder}
        triggerProps={{ "aria-required": required || undefined }}
      />
      <CredentialsModal
        onCredentialCreated={(id) => {
          onChange(id);
          setIsOpen(false);
        }}
      />
    </>
  );
}

export { CredentialSelector };
