import { Input } from "@/components/ui/input";
import { useLayoutEffect, useRef } from "react";

type Props = React.ComponentPropsWithoutRef<typeof Input>;

function HorizontallyResizingInput(props: Props) {
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
      size={1}
      onInput={(event) => {
        setSize();
        props.onInput?.(event);
      }}
      ref={ref}
      onKeyDown={(event) => {
        setSize();
        props.onKeyDown?.(event);
      }}
      {...props}
    />
  );
}

export { HorizontallyResizingInput };
