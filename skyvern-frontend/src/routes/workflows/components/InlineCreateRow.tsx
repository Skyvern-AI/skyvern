import { PlusIcon, CheckIcon, Cross2Icon } from "@radix-ui/react-icons";
import { useState, KeyboardEvent } from "react";
import { Input } from "@/components/ui/input";

type Props = {
  label: string;
  placeholder: string;
  isSubmitting: boolean;
  onConfirm: (value: string) => void;
};

function InlineCreateRow({
  label,
  placeholder,
  isSubmitting,
  onConfirm,
}: Props) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState("");

  if (!editing) {
    return (
      <button
        type="button"
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs text-slate-200 hover:bg-slate-700"
        onClick={() => setEditing(true)}
        disabled={isSubmitting}
      >
        <PlusIcon className="size-4" />
        <span>{label}</span>
      </button>
    );
  }

  const handleKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter" && value.trim()) {
      e.preventDefault();
      onConfirm(value.trim());
    } else if (e.key === "Escape") {
      e.preventDefault();
      setEditing(false);
      setValue("");
    }
  };

  return (
    <div className="flex items-center gap-1 px-2 py-1.5">
      <Input
        autoFocus
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={handleKeyDown}
        placeholder={placeholder}
        className="h-7 text-xs"
        disabled={isSubmitting}
      />
      <button
        type="button"
        className="rounded p-1 text-slate-300 hover:bg-slate-700 disabled:opacity-50"
        onClick={() => value.trim() && onConfirm(value.trim())}
        disabled={isSubmitting || !value.trim()}
        aria-label="Confirm"
      >
        <CheckIcon className="size-4" />
      </button>
      <button
        type="button"
        className="rounded p-1 text-slate-400 hover:bg-slate-700"
        onClick={() => {
          setEditing(false);
          setValue("");
        }}
        disabled={isSubmitting}
        aria-label="Cancel"
      >
        <Cross2Icon className="size-4" />
      </button>
    </div>
  );
}

export { InlineCreateRow };
