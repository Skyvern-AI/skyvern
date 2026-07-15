import { OnePasswordIcon } from "@/components/icons/OnePasswordIcon";
import {
  CustomSelectItem,
  Select,
  SelectContent,
  SelectItemText,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { useMemo } from "react";
import { useOnePasswordItemsQuery } from "../hooks/useOnePasswordItemsQuery";

type Props = {
  vaultId: string;
  itemId: string;
  onSelect: (vaultId: string, itemId: string) => void;
  credentialDataType: "password" | "secret" | "creditCard";
};

function itemValue(vaultId: string, itemId: string) {
  return `${vaultId}:${itemId}`;
}

function OnePasswordItemSelector({
  vaultId,
  itemId,
  onSelect,
  credentialDataType,
}: Props) {
  const { data, isLoading, isError } = useOnePasswordItemsQuery();

  const filteredItems = useMemo(() => {
    const items = data?.items ?? [];
    const filtered = items.filter((item) => {
      const category = item.category.toLowerCase();

      if (credentialDataType === "password") {
        return category.includes("login") || category.includes("password");
      }

      if (credentialDataType === "creditCard") {
        return category.includes("card");
      }

      return true;
    });

    return filtered;
  }, [credentialDataType, data?.items]);

  // Always include the currently-selected item so a saved selection renders even when the
  // category filter would otherwise hide it (otherwise the picker shows blank while the IDs persist).
  const visibleItems = useMemo(() => {
    const items = data?.items ?? [];
    const current = items.find(
      (item) => item.vault_id === vaultId && item.item_id === itemId,
    );
    if (
      current &&
      !filteredItems.some(
        (item) => item.vault_id === vaultId && item.item_id === itemId,
      )
    ) {
      return [current, ...filteredItems];
    }
    return filteredItems;
  }, [filteredItems, data?.items, vaultId, itemId]);

  if (isLoading) {
    return <Skeleton className="h-10 w-full" />;
  }

  const currentValue = itemValue(vaultId, itemId);
  const selectedValue = visibleItems.some(
    (item) => itemValue(item.vault_id, item.item_id) === currentValue,
  )
    ? currentValue
    : "";

  return (
    <Select
      value={selectedValue}
      onValueChange={(value) => {
        const separatorIndex = value.indexOf(":");
        if (separatorIndex === -1) {
          return;
        }

        onSelect(
          value.slice(0, separatorIndex),
          value.slice(separatorIndex + 1),
        );
      }}
    >
      <SelectTrigger>
        <SelectValue placeholder="Select a 1Password item" />
      </SelectTrigger>
      <SelectContent>
        {isError && (
          <div className="px-2 py-1.5 text-xs text-muted-foreground">
            Couldn&apos;t load 1Password items. Enter a value manually instead.
          </div>
        )}
        {!isError && !data?.configured && (
          <div className="px-2 py-1.5 text-xs text-muted-foreground">
            Connect 1Password in Settings to list items
          </div>
        )}
        {!isError && data?.configured && visibleItems.length === 0 && (
          <div className="px-2 py-1.5 text-xs text-muted-foreground">
            No 1Password items found
          </div>
        )}
        {visibleItems.map((item) => (
          <CustomSelectItem
            key={itemValue(item.vault_id, item.item_id)}
            value={itemValue(item.vault_id, item.item_id)}
          >
            <div className="flex min-w-0 items-center gap-2">
              <OnePasswordIcon className="size-4" />
              <span className="min-w-0 flex-1 truncate text-sm font-medium">
                <SelectItemText>{item.title}</SelectItemText>
              </span>
              <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                {item.vault_name}
              </span>
            </div>
          </CustomSelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

export { OnePasswordItemSelector };
