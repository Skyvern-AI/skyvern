import { Skeleton } from "@/components/ui/skeleton";
import { CredentialItem } from "./CredentialItem";
import { useCredentialsQuery } from "@/routes/workflows/hooks/useCredentialsQuery";
import {
  Pagination,
  PaginationContent,
  PaginationItem,
  PaginationLink,
  PaginationNext,
  PaginationPrevious,
} from "@/components/ui/pagination";
import { cn } from "@/util/utils";
import { useState } from "react";

type CredentialFilter = "password" | "credit_card" | "secret";

type Props = {
  filter?: CredentialFilter;
};

const EMPTY_MESSAGE: Record<CredentialFilter, string> = {
  password: "No password credentials stored yet.",
  credit_card: "No credit cards stored yet.",
  secret: "No secrets stored yet.",
};

const PAGE_SIZE = 25;

function CredentialsList({ filter }: Props = {}) {
  const [page, setPage] = useState(1);
  const { data: credentials, isLoading } = useCredentialsQuery({
    page,
    page_size: PAGE_SIZE,
  });

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

  if (filteredCredentials.length === 0 && page === 1) {
    return (
      <div className="rounded-md border border-slate-700 bg-slate-elevation1 p-6 text-sm text-slate-300">
        {filter ? EMPTY_MESSAGE[filter] : "No credentials stored yet."}
      </div>
    );
  }

  const hasNextPage = credentials.length === PAGE_SIZE;

  return (
    <div className="space-y-5">
      <div className="space-y-5">
        {filteredCredentials.map((credential) => (
          <CredentialItem
            key={credential.credential_id}
            credential={credential}
          />
        ))}
      </div>
      {(page > 1 || hasNextPage) && (
        <Pagination>
          <PaginationContent>
            <PaginationItem>
              <PaginationPrevious
                className={cn({ "cursor-not-allowed": page === 1 })}
                onClick={() => {
                  if (page > 1) {
                    setPage((prev) => Math.max(1, prev - 1));
                  }
                }}
              />
            </PaginationItem>
            <PaginationItem>
              <PaginationLink>{page}</PaginationLink>
            </PaginationItem>
            <PaginationItem>
              <PaginationNext
                className={cn({ "cursor-not-allowed": !hasNextPage })}
                onClick={() => {
                  if (hasNextPage) {
                    setPage((prev) => prev + 1);
                  }
                }}
              />
            </PaginationItem>
          </PaginationContent>
        </Pagination>
      )}
    </div>
  );
}

export { CredentialsList };
