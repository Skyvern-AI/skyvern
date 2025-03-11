import { Skeleton } from "@/components/ui/skeleton";
import { CredentialItem } from "./CredentialItem";
import { useCredentialsQuery } from "@/routes/workflows/hooks/useCredentialsQuery";

function CredentialsList() {
  const { data: credentials, isLoading } = useCredentialsQuery();

  if (isLoading) {
    return (
      <div className="space-y-5">
        <Skeleton className="h-20 w-full" />
        <Skeleton className="h-20 w-full" />
      </div>
    );
  }

  if (!credentials) {
    return null;
  }

  return (
    <div className="space-y-5">
      {credentials.map((credential) => (
        <CredentialItem
          key={credential.credential_id}
          credential={credential}
        />
      ))}
    </div>
  );
}

export { CredentialsList };
