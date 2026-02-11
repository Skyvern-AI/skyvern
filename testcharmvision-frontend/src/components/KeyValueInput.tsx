import { PlusIcon, Cross2Icon } from "@radix-ui/react-icons";
import { useEffect, useState } from "react";
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

  useEffect(() => {
    const obj: Record<string, string> = {};
    let hasDuplicateKey = false;

    for (const { key, value } of pairs) {
      if (!key) {
        continue;
      }
      if (key in obj) {
        hasDuplicateKey = true;
        continue;
      }
      obj[key] = value;
    }

    if (!hasDuplicateKey) {
      const output =
        typeof value === "string"
          ? Object.keys(obj).length
            ? JSON.stringify(obj)
            : ""
          : Object.keys(obj).length
            ? obj
            : null;
      onChange(output);
    }
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
      e.relatedTarget === null ||
      (e.currentTarget &&
        e.relatedTarget &&
        !e.currentTarget.contains(e.relatedTarget as Node))
    ) {
      const obj: Record<string, string> = {};
      const reversedPairs = [...pairs].reverse();
      for (const { key, value } of reversedPairs) {
        if (!key && !value) {
          continue;
        }
        if (key) {
          if (key in obj) {
            const oldValue = value;
            const newValue = obj[key];
            toast({
              variant: "warning",
              title: `Duplicate Header ('${key}')`,
              description: `Header '${key}' already existed. It was changed from '${oldValue}' to '${newValue}'.`,
            });
            continue;
          }
          obj[key] = value;
        }
      }

      const reversedObj = Object.fromEntries(Object.entries(obj).reverse());

      const output =
        typeof value === "string"
          ? Object.keys(reversedObj).length
            ? JSON.stringify(reversedObj)
            : ""
          : Object.keys(reversedObj).length
            ? reversedObj
            : null;

      onChange(output);

      setPairs(
        Object.entries(reversedObj).map(([key, value]) => ({
          id: nanoid(),
          key,
          value,
        })),
      );
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
