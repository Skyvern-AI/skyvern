import { useEffect, useMemo, useState } from "react";
import { Cross2Icon, PlusIcon } from "@radix-ui/react-icons";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "@/util/utils";
import {
  newEntryId,
  parseColumnMapping,
  resolveDestination,
  serializeColumnMapping,
  type ColumnMappingEntry,
} from "@/util/columnMappingSerialization";

type Header = { letter: string; name: string };

type Props = {
  value: string;
  onChange: (nextJson: string) => void;
  headers?: Header[];
  headersLoading?: boolean;
  disabled?: boolean;
  // Stable id (e.g. node id) used to namespace the per-row datalist so two
  // editors visible at once can't share a datalist via colliding ids.
  idScope: string;
};

function ColumnMappingEditor({
  value,
  onChange,
  headers = [],
  headersLoading = false,
  disabled = false,
  idScope,
}: Props) {
  const withIds = (items: ColumnMappingEntry[]): ColumnMappingEntry[] =>
    items.map((e) => ({ ...e, _id: e._id ?? newEntryId() }));

  const [entries, setEntries] = useState<ColumnMappingEntry[]>(() =>
    withIds(parseColumnMapping(value)),
  );

  // Re-sync if the external value changes (e.g. template restore).
  useEffect(() => {
    const incoming = parseColumnMapping(value);
    const serializedIncoming = serializeColumnMapping(incoming);
    const serializedCurrent = serializeColumnMapping(entries);
    if (serializedIncoming !== serializedCurrent) {
      setEntries(withIds(incoming));
    }
    // Intentionally excludes `entries` - we only sync external -> internal
    // when the parent value changes. Including it would re-run on every
    // internal edit and loop.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);

  const headerOptions = useMemo(
    () =>
      headers.map((h) => ({
        value: h.letter,
        label: `${h.letter} - ${h.name}`,
      })),
    [headers],
  );

  const commit = (next: ColumnMappingEntry[]) => {
    setEntries(next);
    onChange(serializeColumnMapping(next));
  };

  const updateKey = (index: number, key: string) => {
    const next = entries.map((e, i) => (i === index ? { ...e, key } : e));
    commit(next);
  };

  const updateLetter = (index: number, rawLetter: string) => {
    const resolved = resolveDestination(rawLetter, headers);
    const next = entries.map((e, i) =>
      i === index ? { ...e, letter: resolved } : e,
    );
    commit(next);
  };

  const addRow = () => {
    commit([...entries, { key: "", letter: "", _id: newEntryId() }]);
  };

  const removeRow = (index: number) => {
    commit(entries.filter((_, i) => i !== index));
  };

  if (entries.length === 0) {
    return (
      <div className="rounded-md border border-dashed border-slate-700 bg-slate-900/40 p-3 text-xs text-slate-400">
        <div className="mb-2">
          No column mappings. Add mappings when your data is a list of objects;
          each source field maps to one sheet column.
        </div>
        <Button
          type="button"
          size="sm"
          variant="secondary"
          disabled={disabled}
          onClick={addRow}
        >
          <PlusIcon className="mr-1 size-3" />
          Add first mapping
        </Button>
      </div>
    );
  }

  return (
    <div className={cn("space-y-2", disabled && "opacity-60")}>
      <div className="grid grid-cols-[1fr_1fr_auto] gap-2 text-[0.65rem] uppercase tracking-wider text-slate-500">
        <Label className="text-[0.65rem]">Source field</Label>
        <Label className="text-[0.65rem]">
          Destination
          {headersLoading ? " (loading headers...)" : null}
        </Label>
        <span />
      </div>
      {entries.map((entry, index) => {
        const rowId = entry._id ?? `row-${index}`;
        return (
          <div
            key={rowId}
            className="grid grid-cols-[1fr_1fr_auto] items-center gap-2"
          >
            <Input
              value={entry.key}
              placeholder="e.g. name"
              className="nopan h-8 text-xs"
              disabled={disabled}
              onChange={(e) => updateKey(index, e.target.value)}
            />
            <Input
              list={`column-mapping-headers-${idScope}-${rowId}`}
              value={entry.letter}
              placeholder={headers.length > 0 ? "A - Name" : "A"}
              className="nopan h-8 text-xs"
              disabled={disabled}
              onChange={(e) => updateLetter(index, e.target.value)}
            />
            {headerOptions.length > 0 ? (
              <datalist id={`column-mapping-headers-${idScope}-${rowId}`}>
                {headerOptions.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </datalist>
            ) : null}
            <Button
              type="button"
              size="icon"
              variant="ghost"
              aria-label="Remove mapping"
              disabled={disabled}
              onClick={() => removeRow(index)}
            >
              <Cross2Icon className="size-3" />
            </Button>
          </div>
        );
      })}
      <Button
        type="button"
        size="sm"
        variant="secondary"
        disabled={disabled}
        onClick={addRow}
      >
        <PlusIcon className="mr-1 size-3" />
        Add mapping
      </Button>
    </div>
  );
}

export { ColumnMappingEditor };
export type { Header as ColumnMappingHeader };
