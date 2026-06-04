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
  search?: string;
  folderId?: string | null;
  isResolvingFolder?: boolean;
  onStartBackgroundTest?: (
    credentialId: string,
    url: string,
    userContext?: string,
  ) => void;
};

const EMPTY_MESSAGE: Record<CredentialFilter, string> = {
  password: "No password credentials stored yet.",
  credit_card: "No credit cards stored yet.",
  secret: "No secrets stored yet.",
};

const PAGE_SIZE = 25;

function CredentialsList({
  filter,
  search,
  folderId,
  isResolvingFolder,
  onStartBackgroundTest,
}: Props = {}) {
  const trimmedSearch = search?.trim() ?? "";
  const [page, setPage] = useState(1);
  const [prevSearch, setPrevSearch] = useState(trimmedSearch);
  const [prevFolderId, setPrevFolderId] = useState(folderId);

  // Reset to page 1 synchronously when the search or folder filter changes —
  // avoids the extra fetch with the stale page that a post-render effect would trigger.
  if (prevSearch !== trimmedSearch || prevFolderId !== folderId) {
    setPrevSearch(trimmedSearch);
    setPrevFolderId(folderId);
    setPage(1);
  }

  const { data: credentials, isLoading } = useCredentialsQuery({
    page,
    page_size: PAGE_SIZE,
    credential_type: filter,
    search: trimmedSearch || undefined,
    folder_id: folderId || undefined,
    // Hold the query until a deep-linked ?folder= slug resolves, so the
    // unfiltered credential bank doesn't flash before the filter applies.
    enabled: !isResolvingFolder,
  });

  if (isResolvingFolder || isLoading) {
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

  if (credentials.length === 0 && page === 1) {
    const emptyMessage = trimmedSearch
      ? `No credentials match “${trimmedSearch}”.`
      : filter
        ? EMPTY_MESSAGE[filter]
        : "No credentials stored yet.";
    return (
      <div className="rounded-md border border-slate-700 bg-slate-elevation1 p-6 text-sm text-neutral-600 dark:text-slate-300">
        {emptyMessage}
      </div>
    );
  }

  const hasNextPage = credentials.length === PAGE_SIZE;

  return (
    <div className="space-y-5">
      <div className="space-y-5">
        {credentials.map((credential) => (
          <CredentialItem
            key={credential.credential_id}
            credential={credential}
            onStartBackgroundTest={onStartBackgroundTest}
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
