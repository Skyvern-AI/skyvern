import { getClient } from "@/api/AxiosClient";
import { CredentialApiResponse } from "@/api/types";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";

type Props = {
  value: string;
  onChange: (value: string) => void;
};

function CredentialSelector({ value, onChange }: Props) {
  const credentialGetter = useCredentialGetter();

  const { data: credentials, isFetching } = useQuery<
    Array<CredentialApiResponse>
  >({
    queryKey: ["credentials"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return await client.get("/credentials").then((res) => res.data);
    },
  });

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
