import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { useCredentialsQuery } from "../hooks/useCredentialsQuery";

type Props = {
  value: string;
  onChange: (value: string) => void;
};

function CredentialSelector({ value, onChange }: Props) {
  const { data: credentials, isFetching } = useCredentialsQuery();

  if (isFetching) {
    return <Skeleton className="h-10 w-full" />;
  }

  if (!credentials) {
    return null;
  }

  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger>
        <SelectValue placeholder="Select a credential" />
      </SelectTrigger>
      <SelectContent>
        {credentials.map((credential) => (
          <SelectItem
            key={credential.credential_id}
            value={credential.credential_id}
          >
            <div className="space-y-2">
              <p className="text-sm font-medium">{credential.name}</p>
              <p className="text-xs text-slate-400">
                {credential.credential_type === "password"
                  ? "Password"
                  : "Credit Card"}
              </p>
            </div>
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

export { CredentialSelector };
