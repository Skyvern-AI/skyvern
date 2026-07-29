import type { CredentialApiResponse } from "@/api/types";
import { Button, type ButtonProps } from "@/components/ui/button";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { Skeleton } from "@/components/ui/skeleton";
import {
  isCredentialNotFoundError,
  useCredentialQuery,
} from "@/routes/workflows/hooks/useCredentialQuery";
import { cn } from "@/util/utils";
import { keepPreviousData } from "@tanstack/react-query";
import {
  CheckIcon,
  ChevronDownIcon,
  ExclamationTriangleIcon,
  PlusIcon,
} from "@radix-ui/react-icons";
import { type ReactNode, useMemo, useState } from "react";
import { useDebounce } from "use-debounce";
import { useCredentialsQuery } from "../hooks/useCredentialsQuery";

type CredentialComboboxExtraOption = {
  value: string;
  label: string;
  searchText?: string;
  content?: ReactNode;
};

type CredentialComboboxSelection =
  | { type: "credential"; credential: CredentialApiResponse }
  | { type: "extra"; option: CredentialComboboxExtraOption };

type SelectedValueState = {
  credential: CredentialApiResponse | undefined;
  extraOption: CredentialComboboxExtraOption | undefined;
  isLoading: boolean;
  isNotFound: boolean;
  isError: boolean;
};

type QuerySettings = {
  enabled?: boolean;
  vaultType?: string;
  excludeCredentialIds?: ReadonlySet<string>;
};

type Props = {
  value?: string;
  onValueChange: (
    value: string,
    selection: CredentialComboboxSelection,
  ) => void;
  selectedCredentialId?: string;
  extraOptions?: CredentialComboboxExtraOption[];
  onAddNew?: () => void;
  renderCredentialItem?: (credential: CredentialApiResponse) => ReactNode;
  renderSelectedValue?: (state: SelectedValueState) => ReactNode;
  placeholder?: string;
  disabled?: boolean;
  query?: QuerySettings;
  triggerProps?: Pick<ButtonProps, "aria-required" | "className">;
};

function CredentialCombobox({
  value,
  onValueChange,
  selectedCredentialId,
  extraOptions = [],
  onAddNew,
  renderCredentialItem,
  renderSelectedValue,
  placeholder = "Select a credential",
  disabled,
  query: querySettings,
  triggerProps,
}: Props) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [debouncedQuery] = useDebounce(query, 300);
  const {
    data: credentials = [],
    isFetching,
    isLoading,
  } = useCredentialsQuery({
    enabled: querySettings?.enabled,
    page_size: 100,
    vault_type: querySettings?.vaultType,
    search: debouncedQuery.trim() || undefined,
    placeholderData: keepPreviousData,
  });

  const selectedExtraOption = extraOptions.find(
    (option) => option.value === value,
  );
  const resolvedSelectedCredentialId =
    selectedCredentialId ?? (selectedExtraOption || !value ? undefined : value);
  const selectedCredentialFromList = credentials.find(
    (credential) => credential.credential_id === resolvedSelectedCredentialId,
  );
  const selectedCredentialQuery = useCredentialQuery(
    resolvedSelectedCredentialId,
    {
      enabled: querySettings?.enabled !== false && !selectedCredentialFromList,
    },
  );
  const selectedCredential =
    selectedCredentialFromList ?? selectedCredentialQuery.data;
  const isNotFound = isCredentialNotFoundError(selectedCredentialQuery.error);
  const selectedValueState: SelectedValueState = {
    credential: selectedCredential,
    extraOption: selectedExtraOption,
    isLoading:
      Boolean(resolvedSelectedCredentialId) &&
      querySettings?.enabled !== false &&
      !selectedCredential &&
      selectedCredentialQuery.isLoading,
    isNotFound,
    isError: selectedCredentialQuery.isError && !isNotFound,
  };
  const customSelectedValue = renderSelectedValue?.(selectedValueState);

  const filteredExtraOptions = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    if (!normalizedQuery) {
      return extraOptions;
    }
    return extraOptions.filter((option) =>
      (option.searchText ?? option.label)
        .toLowerCase()
        .includes(normalizedQuery),
    );
  }, [extraOptions, query]);

  const visibleCredentials = credentials.filter(
    (credential) =>
      !querySettings?.excludeCredentialIds?.has(credential.credential_id),
  );

  const close = () => {
    setOpen(false);
    setQuery("");
  };

  const selectedContent =
    customSelectedValue ??
    selectedExtraOption?.content ??
    selectedExtraOption?.label ??
    selectedCredential?.name ??
    (selectedValueState.isLoading ? (
      "Loading credential..."
    ) : selectedValueState.isNotFound ? (
      <span className="flex items-center gap-2 text-red-500">
        <ExclamationTriangleIcon className="size-4 shrink-0" />
        Credential not found
      </span>
    ) : selectedValueState.isError ? (
      <span className="flex items-center gap-2 text-red-500">
        <ExclamationTriangleIcon className="size-4 shrink-0" />
        Couldn't load credential.
      </span>
    ) : (
      placeholder
    ));

  if (isLoading) {
    return <Skeleton className="h-10 w-full" />;
  }

  return (
    <Popover
      open={open}
      onOpenChange={(nextOpen) => {
        setOpen(nextOpen);
        if (!nextOpen) {
          setQuery("");
        }
      }}
    >
      <PopoverTrigger asChild>
        <Button
          type="button"
          role="combobox"
          aria-label={
            selectedExtraOption?.label ??
            selectedCredential?.name ??
            placeholder
          }
          aria-expanded={open}
          variant="outline"
          disabled={disabled}
          {...triggerProps}
          className={cn(
            "w-full justify-between px-3 font-normal",
            triggerProps?.className,
          )}
        >
          <div className="min-w-0 flex-1 overflow-hidden text-left">
            {selectedContent}
          </div>
          <ChevronDownIcon className="ml-2 size-4 shrink-0 opacity-50" />
        </Button>
      </PopoverTrigger>
      <PopoverContent
        className="w-[var(--radix-popover-trigger-width)] p-0"
        align="start"
      >
        <Command shouldFilter={false}>
          <CommandInput
            placeholder="Search credentials..."
            value={query}
            onValueChange={setQuery}
          />
          <CommandList>
            {onAddNew && !query.trim() ? (
              <CommandGroup>
                <CommandItem
                  value="add-new-credential"
                  onSelect={() => {
                    close();
                    onAddNew();
                  }}
                >
                  <PlusIcon className="mr-2 size-4" />
                  <span>Add new credential</span>
                </CommandItem>
              </CommandGroup>
            ) : null}
            {visibleCredentials.length === 0 &&
            filteredExtraOptions.length === 0 ? (
              <CommandEmpty>
                {isFetching
                  ? "Searching credentials..."
                  : "No credentials found."}
              </CommandEmpty>
            ) : null}
            {visibleCredentials.length > 0 ? (
              <CommandGroup>
                {visibleCredentials.map((credential) => (
                  <CommandItem
                    key={credential.credential_id}
                    value={`credential-${credential.credential_id}`}
                    onSelect={() => {
                      close();
                      onValueChange(credential.credential_id, {
                        type: "credential",
                        credential,
                      });
                    }}
                  >
                    <div className="min-w-0 flex-1">
                      {renderCredentialItem?.(credential) ?? credential.name}
                    </div>
                    {credential.credential_id ===
                    resolvedSelectedCredentialId ? (
                      <CheckIcon className="ml-2 size-4 shrink-0" />
                    ) : null}
                  </CommandItem>
                ))}
              </CommandGroup>
            ) : null}
            {filteredExtraOptions.length > 0 ? (
              <CommandGroup>
                {filteredExtraOptions.map((option) => (
                  <CommandItem
                    key={option.value}
                    value={`extra-${option.value}`}
                    onSelect={() => {
                      close();
                      onValueChange(option.value, {
                        type: "extra",
                        option,
                      });
                    }}
                  >
                    <div className="min-w-0 flex-1">
                      {option.content ?? option.label}
                    </div>
                    {option.value === value ? (
                      <CheckIcon className="ml-2 size-4 shrink-0" />
                    ) : null}
                  </CommandItem>
                ))}
              </CommandGroup>
            ) : null}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}

export { CredentialCombobox };
export type {
  CredentialComboboxExtraOption,
  CredentialComboboxSelection,
  SelectedValueState,
};
