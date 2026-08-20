import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

import { CopyButton } from "@/components/CopyButton";
import { Button } from "@/components/ui/button";

import { JsonExplorer } from "../../workflowRun/blockDetail/BlockInspector";

// Clamped, copyable text box shared by primitive run field values and block
// prompts. line-clamp-6 caps the collapsed height so a long pasted document or
// serialized payload can't make the pane arbitrarily tall; Show more/Show less
// appears only when the text actually overflows the clamp.
export function ClampedProse({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false);
  const [overflows, setOverflows] = useState(false);
  const contentId = useId();
  const boxRef = useRef<HTMLDivElement>(null);
  const measureOverflow = useCallback(() => {
    const element = boxRef.current;
    if (element) {
      setOverflows(element.scrollHeight > element.clientHeight);
    }
  }, []);

  useLayoutEffect(() => {
    if (!expanded) {
      measureOverflow();
    }
  }, [expanded, measureOverflow, text]);

  useEffect(() => {
    if (expanded || typeof ResizeObserver === "undefined") {
      return;
    }
    const element = boxRef.current;
    if (!element) {
      return;
    }
    // Width changes (pane resize) re-wrap the text and can flip the overflow
    // verdict even though the collapsed clamp is pinned — so keep observing.
    const observer = new ResizeObserver(measureOverflow);
    observer.observe(element);
    return () => observer.disconnect();
  }, [expanded, measureOverflow]);

  return (
    <div className="flex flex-col items-start">
      <div className="group relative w-full">
        <CopyButton
          value={text}
          className="absolute right-1.5 top-1.5 z-10 h-6 w-6 text-muted-foreground opacity-0 transition-opacity hover:text-foreground focus-visible:opacity-100 group-hover:opacity-100"
        />
        {/* elevation2 (the run-view pane is elevation1) so the field reads as
            raised. Padding sits on this wrapper, not the clamped child, because
            line-clamp clips at the padding box and would otherwise leak a sliver
            of the next line below the ellipsis. */}
        <div className="rounded bg-slate-elevation2 px-2.5 py-2 pr-9">
          <div
            id={contentId}
            ref={boxRef}
            className={`whitespace-pre-wrap break-words text-xs text-tertiary-foreground ${expanded ? "" : "line-clamp-6"}`}
          >
            {text}
          </div>
        </div>
      </div>
      {overflows ? (
        <Button
          type="button"
          variant="link"
          size="sm"
          className="mt-1.5 h-auto justify-start p-0 text-xs text-muted-foreground hover:text-foreground"
          aria-controls={contentId}
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? "Show less" : "Show more"}
        </Button>
      ) : null}
    </div>
  );
}

export function RunFieldValue({
  value,
  label,
}: {
  value: unknown;
  label: string;
}) {
  if (typeof value === "object" && value !== null) {
    return <JsonExplorer value={value} rootLabel={label} />;
  }
  // Nullish/empty stays distinguishable from the literal string "null" (which
  // String() would flatten into the same prose); 0 and false are real values.
  if (value === null || value === undefined || value === "") {
    return <span className="text-xs italic text-muted-foreground">—</span>;
  }
  return <ClampedProse text={String(value)} />;
}
