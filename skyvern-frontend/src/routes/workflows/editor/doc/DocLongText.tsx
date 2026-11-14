import {
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type ReactElement,
} from "react";
import { flushSync } from "react-dom";

import { DOC_COLLAPSED_LINES, docTextOverflows } from "./docTextFold";

export type DocTextCollapse = {
  open: boolean;
  setOpen: (open: boolean) => void;
  // The step's name, for the control's accessible name.
  name: string;
};

export type DocShownText = {
  // The viewing or read-only span's element; the editing span is never measured.
  ref: (el: HTMLSpanElement | null) => void;
  id: string;
  style: CSSProperties | undefined;
};

const CLAMPED: CSSProperties = {
  display: "-webkit-box",
  WebkitBoxOrient: "vertical",
  WebkitLineClamp: DOC_COLLAPSED_LINES,
  overflow: "hidden",
};

function scrollParent(el: HTMLElement | null): HTMLElement | null {
  for (let at = el?.parentElement; at; at = at.parentElement) {
    if (/(auto|scroll)/.test(getComputedStyle(at).overflowY)) return at;
  }
  return null;
}

/**
 * The one place a step's long text is shortened: its first lines with an ellipsis and a Show more control.
 * Display only: the text is never cut, and the editing line always holds all of it.
 */
export function DocLongText({
  collapse,
  value,
  children,
}: {
  collapse: DocTextCollapse | undefined;
  value: string;
  children: (shown: DocShownText) => ReactElement;
}) {
  const [el, setEl] = useState<HTMLSpanElement | null>(null);
  const control = useRef<HTMLButtonElement>(null);
  const id = useId();
  const [overflows, setOverflows] = useState(false);
  const enabled = collapse !== undefined;
  useLayoutEffect(() => {
    if (!enabled || !el) return;
    const measure = () => setOverflows(docTextOverflows(el));
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [enabled, value, el]);
  const folded = enabled && !collapse.open;
  const toggle = () => {
    if (!collapse) return;
    if (!collapse.open) {
      collapse.setOpen(true);
      return;
    }
    // Folding pulls the control up by the hidden lines; scroll by as much, so it stays under the pointer.
    const before = control.current?.getBoundingClientRect().top ?? 0;
    flushSync(() => collapse.setOpen(false));
    const after = control.current?.getBoundingClientRect().top ?? before;
    const scroller = scrollParent(control.current);
    if (scroller) scroller.scrollTop += after - before;
  };
  return (
    <>
      {children({
        ref: setEl,
        id,
        style: folded && overflows ? CLAMPED : undefined,
      })}
      {enabled && overflows && (
        <button
          ref={control}
          type="button"
          data-doc-collapse
          aria-expanded={collapse.open}
          aria-controls={id}
          aria-label={`${collapse.open ? "Show less" : "Show more"}: ${collapse.name}`}
          onClick={toggle}
          className="-ml-1 rounded px-1 text-[12.5px] leading-[1.6] text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
        >
          {collapse.open ? "Show less" : "Show more"}
        </button>
      )}
    </>
  );
}
