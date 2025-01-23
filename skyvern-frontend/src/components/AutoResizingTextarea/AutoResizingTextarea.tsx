import { ChangeEventHandler, useEffect, useLayoutEffect, useRef } from "react";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/util/utils";

type Props = {
  value: string;
  onChange?: ChangeEventHandler<HTMLTextAreaElement>;
  className?: string;
  readOnly?: boolean;
  placeholder?: string;
};

function AutoResizingTextarea({
  value,
  onChange,
  className,
  readOnly,
  placeholder,
}: Props) {
  const ref = useRef<HTMLTextAreaElement>(null);

  useLayoutEffect(() => {
    // size the textarea correctly on first render
    if (!ref.current) {
      return;
    }
    ref.current.style.height = `${ref.current.scrollHeight + 2}px`;
  }, []);

  useEffect(() => {
    if (!ref.current) {
      return;
    }
    ref.current.style.height = "auto";
    ref.current.style.height = `${ref.current.scrollHeight + 2}px`;
  }, [value]);

  return (
    <Textarea
      value={value}
      onChange={onChange}
      readOnly={readOnly}
      placeholder={placeholder}
      ref={ref}
      rows={1}
      className={cn("min-h-0 resize-none overflow-y-hidden", className)}
    />
  );
}

export { AutoResizingTextarea };
