import { useLayoutEffect, useRef } from "react";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/util/utils";

type Props = React.ComponentProps<typeof Textarea>;

function AutoResizingTextarea(props: Props) {
  const ref = useRef<HTMLTextAreaElement>(null);

  useLayoutEffect(() => {
    // size the textarea correctly on first render
    if (!ref.current) {
      return;
    }
    ref.current.style.height = `${ref.current.scrollHeight + 2}px`;
  }, []);

  function setSize() {
    if (!ref.current) {
      return;
    }
    ref.current.style.height = "auto";
    ref.current.style.height = `${ref.current.scrollHeight + 2}px`;
  }

  return (
    <Textarea
      {...props}
      onKeyDown={setSize}
      onInput={setSize}
      ref={ref}
      rows={1}
      className={cn("min-h-0 resize-none overflow-y-hidden", props.className)}
    />
  );
}

export { AutoResizingTextarea };
