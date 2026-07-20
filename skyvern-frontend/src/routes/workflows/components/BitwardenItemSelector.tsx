import { BitwardenIcon } from "@/components/icons/BitwardenIcon";
import {
  CustomSelectItem,
  Select,
  SelectContent,
  SelectItemText,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { useBitwardenItemsQuery } from "../hooks/useBitwardenItemsQuery";

type Props = {
  itemId: string;
  onSelect: (collectionId: string | null, itemId: string) => void;
  credentialDataType: "password" | "creditCard";
};

function BitwardenItemSelector({
  itemId,
  onSelect,
  credentialDataType,
}: Props) {
  const { data, isLoading, isError } = useBitwardenItemsQuery();
  const bitwardenItems = data?.items ?? [];
  const items = bitwardenItems.filter((item) =>
    credentialDataType === "password"
      ? item.credential_type === "password"
      : item.credential_type === "credit_card" && item.collection_id,
  );
  const hasUnselectableCreditCards =
    credentialDataType === "creditCard" &&
    bitwardenItems.some(
      (item) => item.credential_type === "credit_card" && !item.collection_id,
    );

  if (isLoading) {
    return <Skeleton className="h-10 w-full" />;
  }

  const selectedValue = items.some((item) => item.item_id === itemId)
    ? itemId
    : "";
  const message = isError
    ? "Couldn't load Bitwarden items. Enter a value manually instead."
    : !data?.configured
      ? "Connect Bitwarden in Settings to list items"
      : items.length === 0
        ? hasUnselectableCreditCards
          ? "No collection-scoped Bitwarden credit cards found"
          : "No Bitwarden items found"
        : null;

  return (
    <Select
      value={selectedValue}
      onValueChange={(value) => {
        const item = items.find((item) => item.item_id === value);
        if (item) {
          onSelect(item.collection_id ?? null, item.item_id);
        }
      }}
    >
      <SelectTrigger>
        <SelectValue placeholder="Select a Bitwarden item" />
      </SelectTrigger>
      <SelectContent>
        {message && (
          <div className="px-2 py-1.5 text-xs text-muted-foreground">
            {message}
          </div>
        )}
        {items.map((item) => (
          <CustomSelectItem key={item.item_id} value={item.item_id}>
            <div className="flex min-w-0 items-center gap-2">
              <BitwardenIcon className="size-4" />
              <span className="min-w-0 flex-1 truncate text-sm font-medium">
                <SelectItemText>{item.title}</SelectItemText>
              </span>
              {item.collection_id && (
                <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                  {item.collection_id}
                </span>
              )}
            </div>
          </CustomSelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

export { BitwardenItemSelector };
