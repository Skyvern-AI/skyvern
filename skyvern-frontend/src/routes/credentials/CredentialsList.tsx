import { Skeleton } from "@/components/ui/skeleton";
import { CredentialItem } from "./CredentialItem";
import { useCredentialsQuery } from "@/routes/workflows/hooks/useCredentialsQuery";

type CredentialFilter = "password" | "credit_card";

type Props = {
  filter?: CredentialFilter;
};

const EMPTY_MESSAGE: Record<CredentialFilter, string> = {
  password: "저장된 비밀번호가 없습니다.",
  credit_card: "저장된 신용카드가 없습니다.",
};

function CredentialsList({ filter }: Props = {}) {
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

  const filteredCredentials = (() => {
    if (!credentials) {
      return [];
    }
    if (!filter) {
      return credentials;
    }
    return credentials.filter(
      (credential) => credential.credential_type === filter,
    );
  })();

  if (filteredCredentials.length === 0) {
    return (
      <div className="rounded-md border border-slate-700 bg-slate-elevation1 p-6 text-sm text-slate-300">
        {filter ? EMPTY_MESSAGE[filter] : "저장된 인증 정보가 없습니다."}
      </div>
    );
  }

  return (
    <div className="space-y-5">
      {filteredCredentials.map((credential) => (
        <CredentialItem
          key={credential.credential_id}
          credential={credential}
        />
      ))}
    </div>
  );
}

export { CredentialsList };
