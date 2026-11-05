import {
  useContext,
  useEffect,
  useMemo,
  useState,
  type RefObject,
} from "react";
import { cn } from "@/util/utils";
import type { AppNode } from "../nodes";
import type { DocItem } from "./buildDocTree";
import { docOutlineEntries } from "./docOutlineEntries";
import { WorkflowDocActionsContext } from "./WorkflowDocActionsContext";

/** Dashes at the right edge, one per step; hovering opens the full list. */
export function DocOutline({
  items,
  byId,
  scrollRef,
}: {
  items: DocItem[];
  byId: Map<string, AppNode>;
  scrollRef: RefObject<HTMLElement>;
}) {
  const actions = useContext(WorkflowDocActionsContext);
  const entries = useMemo(() => docOutlineEntries(items, byId), [items, byId]);
  const [active, setActive] = useState<string | null>(null);
  useEffect(() => {
    const scroller = scrollRef.current;
    if (!scroller) return;
    const update = () => {
      const top = scroller.getBoundingClientRect().top + 120;
      let current = entries[0]?.id ?? null;
      for (const entry of entries) {
        const el = scroller.querySelector(
          `[data-doc-node-id="${CSS.escape(entry.id)}"]`,
        );
        if (el && el.getBoundingClientRect().top < top) current = entry.id;
      }
      setActive(current);
    };
    update();
    scroller.addEventListener("scroll", update, { passive: true });
    return () => scroller.removeEventListener("scroll", update);
  }, [entries, scrollRef]);
  if (entries.length < 8) return null;
  return (
    <nav
      aria-label="Outline"
      className="group/outline absolute right-3 top-1/2 z-10 -translate-y-1/2"
    >
      <div className="flex flex-col items-end gap-[9px] py-1.5">
        {entries.map((entry) => (
          <button
            key={entry.id}
            type="button"
            aria-label={entry.title}
            onClick={() => actions.goTo(entry.id)}
            className={cn(
              "block h-0.5 rounded-[1px] bg-border outline-none hover:bg-foreground focus-visible:bg-foreground focus-visible:ring-2 focus-visible:ring-ring",
              entry.depth ? "w-2.5" : "w-4",
              entry.id === active && "bg-foreground",
            )}
          />
        ))}
      </div>
      <div className="pointer-events-none absolute -right-1.5 top-1/2 max-h-[calc(100vh-160px)] w-[270px] -translate-y-1/2 overflow-auto rounded-[10px] border border-border bg-background px-1.5 py-2 opacity-0 shadow-lg group-focus-within/outline:pointer-events-auto group-focus-within/outline:opacity-100 group-hover/outline:pointer-events-auto group-hover/outline:opacity-100">
        {entries.map((entry) => (
          <button
            key={entry.id}
            type="button"
            aria-label={entry.title}
            onClick={() => actions.goTo(entry.id)}
            style={{ paddingLeft: 10 + entry.depth * 14 }}
            className={cn(
              "block w-full truncate rounded-md py-[5px] pr-2.5 text-left text-[13px] leading-[1.35] text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring",
              entry.id === active && "text-[#2F76A6] dark:text-[#6AACDA]",
            )}
          >
            {entry.title}
          </button>
        ))}
      </div>
    </nav>
  );
}
