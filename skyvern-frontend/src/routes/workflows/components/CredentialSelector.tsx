import {
  CustomSelectItem,
  Select,
  SelectContent,
  SelectItem,
  SelectItemText,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { useCredentialsQuery } from "../hooks/useCredentialsQuery";
import { PlusIcon } from "@radix-ui/react-icons";
import { getHostname } from "@/util/getHostname";
import { CredentialsModal } from "@/routes/credentials/CredentialsModal";
import {
  CredentialModalTypes,
  useCredentialModalState,
} from "@/routes/credentials/useCredentialModalState";

type Props = {
  value: string;
  onChange: (value: string) => void;
};

function CredentialSelector({ value, onChange }: Props) {
  const { setIsOpen, setType } = useCredentialModalState();
  const { data: credentials, isFetching } = useCredentialsQuery({
    page_size: 100, // Reasonable limit for dropdown selector
  });

  if (isFetching) {
    return <Skeleton className="h-10 w-full" />;
  }

  if (!credentials) {
    return null;
  }

  return (
    <>
      <Select
        value={value}
        onValueChange={(value) => {
          if (value === "new") {
            setIsOpen(true);
            setType(CredentialModalTypes.PASSWORD);
          } else {
            onChange(value);
          }
        }}
      >
        <SelectTrigger>
          <SelectValue placeholder="Select a credential" />
        </SelectTrigger>
        <SelectContent>
          {credentials.map((credential) => (
            <CustomSelectItem
              key={credential.credential_id}
              value={credential.credential_id}
            >
              <div className="space-y-2">
                <div className="flex items-center gap-2">
                  <p className="text-sm font-medium">
                    <SelectItemText>{credential.name}</SelectItemText>
                  </p>
                  {credential.browser_profile_id && (
                    <>
                      <span className="rounded bg-green-900/40 px-1.5 py-0.5 text-[10px] text-green-400">
                        saved-profile
                      </span>
                      {credential.tested_url && (
                        <span className="text-[10px] text-muted-foreground">
                          {getHostname(credential.tested_url)}
                        </span>
                      )}
                    </>
                  )}
                </div>
                <p className="text-xs text-slate-400">
                  {credential.credential_type === "password"
                    ? "Password"
                    : credential.credential_type === "credit_card"
                      ? "Credit Card"
                      : "Secret"}
                </p>
              </div>
            </CustomSelectItem>
          ))}
          <SelectItem value="new">
            <div className="flex items-center gap-2">
              <PlusIcon className="size-4" />
              <span>Add new credential</span>
            </div>
          </SelectItem>
        </SelectContent>
      </Select>
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
