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
  const { data: credentials, isFetching } = useCredentialsQuery();

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
                <p className="text-sm font-medium">
                  <SelectItemText>{credential.name}</SelectItemText>
                </p>
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
