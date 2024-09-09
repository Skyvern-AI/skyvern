import { Input } from "@/components/ui/input";
import { cn } from "@/util/utils";
import { useLayoutEffect, useRef } from "react";

type Props = {
  value: string;
  editable: boolean;
  onChange: (value: string) => void;
  className?: string;
};

function EditableNodeTitle({ value, editable, onChange, className }: Props) {
  const ref = useRef<HTMLInputElement>(null);

  useLayoutEffect(() => {
    // size the textarea correctly on first render
    if (!ref.current) {
      return;
    }
    ref.current.style.width = `${ref.current.scrollWidth + 2}px`;
  }, []);

  function setSize() {
    if (!ref.current) {
      return;
    }
    ref.current.style.width = "auto";
    ref.current.style.width = `${ref.current.scrollWidth + 2}px`;
  }

  return (
    <Input
      disabled={!editable}
      ref={ref}
      className={cn("w-fit min-w-fit max-w-64 border-0 px-0", className)}
      onBlur={(event) => {
        if (!editable) {
          event.currentTarget.value = value;
          return;
        }
        onChange(event.target.value);
      }}
      onKeyDown={(event) => {
        if (!editable) {
          return;
        }
        if (event.key === "Enter") {
          event.currentTarget.blur();
        }
        if (event.key === "Escape") {
          event.currentTarget.value = value;
          event.currentTarget.blur();
        }
        setSize();
      }}
      onInput={setSize}
      defaultValue={value}
    />
  );
}

export { EditableNodeTitle };
