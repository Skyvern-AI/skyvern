import { PlusIcon, Cross2Icon } from "@radix-ui/react-icons";
import { useEffect, useRef, useState } from "react";
import { nanoid } from "nanoid";
import { Input } from "./ui/input";
import { Button } from "./ui/button";
import { toast } from "./ui/use-toast";

export type KeyValueInputProps = {
  value: Record<string, string> | string | null;
  onChange: (value: Record<string, string> | string | null) => void;
  addButtonText?: string;
  readOnly?: boolean;
};

type Pair = {
  id: string;
  key: string;
  value: string;
};

type KV = {
  key: string;
  value: string;
};

function parsePairs(value: Record<string, string> | string | null): KV[] {
  if (!value) {
    return [];
  }
  try {
    const obj = typeof value === "string" ? JSON.parse(value) : value;
    if (obj && typeof obj === "object" && !Array.isArray(obj)) {
      return Object.entries(obj).map(([k, v]) => ({
        key: k,
        value: String(v),
      }));
    }
  } catch {
    // ignore
  }
  return [];
}

/** The rows that actually make it into the emitted map: keyed, first-wins. */
function effectiveEntries(pairs: Array<Pair | KV>): KV[] {
  const seen = new Set<string>();
  const entries: KV[] = [];
  for (const { key, value } of pairs) {
    if (!key || seen.has(key)) {
      continue;
    }
    seen.add(key);
    entries.push({ key, value });
  }
  return entries;
}

function sameEntries(a: KV[], b: KV[]): boolean {
  return (
    a.length === b.length &&
    a.every((entry, index) => {
      const other = b[index];
      return other?.key === entry.key && other?.value === entry.value;
    })
  );
}

function serialize(
  entries: KV[],
  asString: boolean,
): Record<string, string> | string | null {
  if (entries.length === 0) {
    return asString ? "" : null;
  }
  const obj = Object.fromEntries(entries.map(({ key, value }) => [key, value]));
  return asString ? JSON.stringify(obj) : obj;
}

function KeyValueInput({
  value,
  onChange,
  addButtonText = "Add",
  readOnly = false,
}: KeyValueInputProps) {
  const [focusLast, setFocusLast] = useState(false);
  const [pairs, setPairs] = useState<Pair[]>(() =>
    parsePairs(value).map((p) => ({ id: nanoid(), ...p })),
  );

  // `value` is read at emit time rather than tracked as a dependency: this
  // component owns the rows once mounted, and re-deriving them from the prop
  // would fight the parent that renders the value we just emitted.
  const valueRef = useRef(value);
  useEffect(() => {
    valueRef.current = value;
  });

  useEffect(() => {
    const hasDuplicateKey =
      new Set(pairs.filter((p) => p.key).map((p) => p.key)).size !==
      pairs.filter((p) => p.key).length;
    if (hasDuplicateKey) {
      return;
    }

    const entries = effectiveEntries(pairs);
    // Mounting must not dirty the form. Serializing `{}` to `""` (or `null` to
    // `null`) and handing that up on mount made merely opening a settings panel
    // write to the parent's form state, re-rendering every field in it.
    if (sameEntries(entries, effectiveEntries(parsePairs(valueRef.current)))) {
      return;
    }

    onChange(serialize(entries, typeof valueRef.current === "string"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pairs]);

  // reset focusLast on next render cycle
  useEffect(() => {
    if (focusLast) {
      setFocusLast(false);
    }
  }, [focusLast]);

  const handleRemove = (id: string) => {
    setPairs((prev) => prev.filter((p) => p.id !== id));
  };

  const handleAdd = () => {
    const newId = nanoid();
    setPairs((prev) => [...prev, { id: newId, key: "", value: "" }]);
    setFocusLast(true);
  };

  /**
   * Fires when the user shifts focus outside the component. Handles:
   *   - duplicate keys
   *   - removing empty entries
   *
   * In the case of duplicates:
   *   - last in wins
   *   - former k/v is removed
   *   - toast is shown, indicating the old value vs the new value for that key
   */
  const handleBlurCapture = (e: React.FocusEvent<HTMLDivElement>) => {
    if (
      !(
        e.relatedTarget === null ||
        (e.currentTarget &&
          e.relatedTarget &&
          !e.currentTarget.contains(e.relatedTarget as Node))
      )
    ) {
      return;
    }

    // Resolved before anything is toasted so every loser is reported against
    // the value that actually survives, and outside the state updater, which
    // React is free to invoke more than once.
    const winners = new Map<string, Pair>();
    for (const pair of pairs) {
      if (pair.key) {
        winners.set(pair.key, pair);
      }
    }

    for (const pair of pairs) {
      const winner = pair.key ? winners.get(pair.key) : undefined;
      if (!winner || winner === pair) {
        continue;
      }
      toast({
        variant: "warning",
        title: `Duplicate Header ('${pair.key}')`,
        description: `Header '${pair.key}' already existed. It was changed from '${pair.value}' to '${winner.value}'.`,
      });
    }

    const next = Array.from(winners.values());
    // Row ids are React keys. Handing back a new array of rows on every
    // focus-out remounted every input, which drops the caret and — when the
    // focused input is the one being removed — re-enters this handler.
    const unchanged =
      next.length === pairs.length &&
      next.every((pair, index) => pair.id === pairs[index]?.id);
    if (!unchanged) {
      setPairs(next);
    }
  };

  return (
    <div className="space-y-2" onBlurCapture={handleBlurCapture}>
      {pairs.map((pair, idx) => (
        <div
          key={pair.id}
          data-pair-id={pair.id}
          className="flex items-center gap-2"
        >
          <Input
            className="flex-1"
            placeholder="Header"
            value={pair.key}
            readOnly={readOnly}
            autoFocus={focusLast && idx === pairs.length - 1}
            onChange={(e) => {
              setPairs((prev) =>
                prev.map((p) =>
                  p.id === pair.id ? { ...p, key: e.target.value } : p,
                ),
              );
            }}
          />
          <Input
            className="flex-1"
            placeholder="Value"
            value={pair.value}
            readOnly={readOnly}
            onChange={(e) => {
              setPairs((prev) =>
                prev.map((p) =>
                  p.id === pair.id ? { ...p, value: e.target.value } : p,
                ),
              );
            }}
          />
          {!readOnly && (
            <Button
              variant="ghost"
              type="button"
              className="p-2"
              onClick={() => handleRemove(pair.id)}
              onKeyDown={(e) => {
                if (
                  e.key === "Tab" &&
                  !e.shiftKey &&
                  !e.altKey &&
                  !e.ctrlKey &&
                  !e.metaKey &&
                  idx === pairs.length - 1
                ) {
                  e.preventDefault();
                  handleAdd();
                }
              }}
            >
              <Cross2Icon />
            </Button>
          )}
        </div>
      ))}
      {!readOnly && (
        <Button
          type="button"
          variant="secondary"
          onClick={handleAdd}
          className="flex items-center gap-2"
        >
          <PlusIcon className="h-4 w-4" /> {addButtonText}
        </Button>
      )}
    </div>
  );
}

export { KeyValueInput };
