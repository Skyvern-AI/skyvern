import { Cross2Icon } from "@radix-ui/react-icons";
import { useEffect } from "react";
import { Button } from "@/components/ui/button";

type Props = {
  count: number; // pass selectedItems.length, never selected.size
  isOperating: boolean;
  onClear: () => void;
  children?: React.ReactNode;
};

function SelectionBar({ count, isOperating, onClear, children }: Props) {
  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      // Radix layers (dialogs, popovers) preventDefault on the Escape that dismisses them.
      if (event.key === "Escape" && !event.defaultPrevented && !isOperating) {
        onClear();
      }
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [isOperating, onClear]);

  return (
    <div
      role="toolbar"
      aria-label="Bulk actions"
      className="fixed inset-x-0 bottom-6 z-50 mx-auto flex w-fit max-w-[calc(100vw-2rem)] flex-wrap items-center gap-1.5 rounded-lg border border-border bg-background px-4 py-2.5 shadow-xl"
    >
      <span className="whitespace-nowrap text-sm text-muted-foreground">
        {isOperating ? "Processing…" : `${count} selected`}
      </span>
      <Button
        size="icon"
        variant="ghost"
        className="h-7 w-7 text-muted-foreground hover:text-foreground"
        onClick={onClear}
        disabled={isOperating}
        aria-label="Clear selection"
      >
        <Cross2Icon className="h-4 w-4" />
      </Button>
      <SelectionBarDivider />
      {children}
    </div>
  );
}

function SelectionBarDivider() {
  return <div className="mx-1 h-6 w-px bg-border" />;
}

export { SelectionBar, SelectionBarDivider };
