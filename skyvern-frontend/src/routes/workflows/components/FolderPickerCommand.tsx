import * as React from "react";
import { CheckIcon, Cross2Icon } from "@radix-ui/react-icons";
import { useDebounce } from "use-debounce";
import { FolderIcon } from "@/components/icons/FolderIcon";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { useFoldersQuery } from "../hooks/useFoldersQuery";

type Props = {
  currentFolderId: string | null;
  // Force-show "Remove from folder" independent of currentFolderId (bulk: any selected has a folder).
  showRemove?: boolean;
  // While a bulk move is in flight, freeze the picker so a second pick can't
  // start a competing move for the same selection.
  disabled?: boolean;
  onSelect: (folderId: string | null) => void;
};

const FOLDER_PICKER_PAGE_SIZE = 50;

// Compact cmdk folder list for the row context menu. Server-side search keeps it
// usable past the page cap; "Remove from folder" shows only when the agent has one.
function FolderPickerCommand({
  currentFolderId,
  showRemove,
  disabled,
  onSelect,
}: Props) {
  const [query, setQuery] = React.useState("");
  const [debouncedQuery] = useDebounce(query, 300);
  const inputRef = React.useRef<HTMLInputElement>(null);

  React.useEffect(() => {
    // Radix MenuSubContent moves focus to its container on open; re-focus the
    // input on the next frame so typing lands in the command, not the menu.
    const frame = requestAnimationFrame(() => inputRef.current?.focus());
    return () => cancelAnimationFrame(frame);
  }, []);

  const { data: folders = [], isFetching } = useFoldersQuery({
    page_size: FOLDER_PICKER_PAGE_SIZE,
    search: debouncedQuery.trim() || undefined,
  });

  return (
    <Command
      shouldFilter={false}
      onKeyDown={(event) => {
        // Let Escape bubble so the menu can close; keep every other key from
        // reaching the parent menu's typeahead so cmdk can handle it.
        if (event.key !== "Escape") {
          event.stopPropagation();
        }
      }}
    >
      <CommandInput
        ref={inputRef}
        placeholder="Search folders…"
        value={query}
        onValueChange={setQuery}
      />
      <CommandList>
        {(showRemove ?? currentFolderId !== null) ? (
          <CommandGroup>
            <CommandItem
              value="__remove__"
              disabled={disabled}
              onSelect={() => onSelect(null)}
            >
              <Cross2Icon className="mr-2 h-4 w-4 text-destructive" />
              Remove from folder
            </CommandItem>
          </CommandGroup>
        ) : null}
        {folders.length === 0 ? (
          <CommandEmpty>
            {isFetching ? "Searching folders…" : "No folders found."}
          </CommandEmpty>
        ) : (
          <CommandGroup heading="Folders">
            {folders.map((folder) => {
              const isCurrent = folder.folder_id === currentFolderId;
              return (
                <CommandItem
                  key={folder.folder_id}
                  value={`folder-${folder.folder_id}`}
                  disabled={isCurrent || disabled}
                  onSelect={() => onSelect(folder.folder_id)}
                >
                  <FolderIcon className="mr-2 h-4 w-4 shrink-0 text-blue-700 dark:text-blue-400" />
                  <span className="truncate">{folder.title}</span>
                  {isCurrent ? (
                    <CheckIcon className="ml-auto h-4 w-4 text-blue-700 dark:text-blue-400" />
                  ) : null}
                </CommandItem>
              );
            })}
          </CommandGroup>
        )}
      </CommandList>
    </Command>
  );
}

export { FolderPickerCommand };
