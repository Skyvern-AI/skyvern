import { useEffect, useRef, useState } from "react";
import { ChevronDownIcon, ChevronUpIcon } from "@radix-ui/react-icons";
import { Button } from "@/components/ui/button";
import type {
  ConsoleResult,
  ConsoleStatus,
} from "@/routes/streaming/useCdpInput";
import { cn } from "@/util/utils";

interface ConsoleEntry {
  id: number;
  expression: string;
  result: ConsoleResult | null;
}

const MAX_ENTRIES = 100;
const MAX_HISTORY = 50;

const STATUS_CLASS: Record<ConsoleStatus, string> = {
  ok: "text-slate-100",
  error: "text-red-400",
  navigated: "text-amber-300",
  timeout: "text-amber-300",
  rejected: "text-amber-300",
  unavailable: "text-amber-300",
};

interface Props {
  canRun: boolean;
  onEvaluate: (expression: string) => Promise<ConsoleResult>;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  onReturnFocus?: () => void;
}

function BrowserSessionConsole({
  canRun,
  onEvaluate,
  open: openProp,
  onOpenChange,
  onReturnFocus,
}: Props) {
  const [internalOpen, setInternalOpen] = useState(false);
  const open = openProp ?? internalOpen;
  const setOpen = (next: boolean) => {
    if (openProp === undefined) {
      setInternalOpen(next);
    }
    onOpenChange?.(next);
  };
  const [value, setValue] = useState("");
  const [running, setRunning] = useState(false);
  const [entries, setEntries] = useState<ConsoleEntry[]>([]);
  const historyRef = useRef<string[]>([]);
  const historyIndexRef = useRef<number | null>(null);
  const nextIdRef = useRef(0);
  const outputRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const wasOpenRef = useRef(open);

  useEffect(() => {
    const wasOpen = wasOpenRef.current;
    wasOpenRef.current = open;
    if (!wasOpen && open && canRun) {
      textareaRef.current?.focus();
    }
  }, [open, canRun]);

  useEffect(() => {
    const output = outputRef.current;
    if (output) {
      output.scrollTop = output.scrollHeight;
    }
  }, [entries, open]);

  async function run() {
    const expression = value;
    if (!canRun || running || !expression.trim()) {
      return;
    }
    nextIdRef.current += 1;
    const id = nextIdRef.current;
    historyRef.current = [
      ...historyRef.current.filter((item) => item !== expression),
      expression,
    ].slice(-MAX_HISTORY);
    historyIndexRef.current = null;
    setValue("");
    setRunning(true);
    setEntries((previous) =>
      [...previous, { id, expression, result: null }].slice(-MAX_ENTRIES),
    );
    const result = await onEvaluate(expression);
    setEntries((previous) =>
      previous.map((entry) => (entry.id === id ? { ...entry, result } : entry)),
    );
    setRunning(false);
  }

  function showHistory(index: number | null) {
    historyIndexRef.current = index;
    setValue(index === null ? "" : (historyRef.current[index] ?? ""));
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void run();
      return;
    }
    const history = historyRef.current;
    const singleLine = !value.includes("\n");
    if (e.key === "ArrowUp" && singleLine && history.length > 0) {
      e.preventDefault();
      const current = historyIndexRef.current;
      showHistory(
        current === null ? history.length - 1 : Math.max(0, current - 1),
      );
    } else if (
      e.key === "ArrowDown" &&
      singleLine &&
      historyIndexRef.current !== null
    ) {
      e.preventDefault();
      const next = historyIndexRef.current + 1;
      showHistory(next >= history.length ? null : next);
    }
  }

  return (
    <div
      className="flex w-full flex-shrink-0 flex-col border-t border-slate-700 bg-slate-900 text-xs"
      onKeyDown={(e) => {
        if (
          e.key === "F12" &&
          !e.altKey &&
          !e.ctrlKey &&
          !e.metaKey &&
          !e.shiftKey
        ) {
          e.preventDefault();
          e.stopPropagation();
          if (!e.repeat) {
            const next = !open;
            setOpen(next);
            if (!next) {
              onReturnFocus?.();
            }
          }
        }
      }}
    >
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex h-8 items-center gap-2 px-3 text-slate-300 hover:text-slate-100"
        aria-expanded={open}
      >
        {open ? (
          <ChevronDownIcon className="h-3 w-3" />
        ) : (
          <ChevronUpIcon className="h-3 w-3" />
        )}
        <span className="font-medium">Console</span>
        <span className="text-slate-500">Run JavaScript in this page</span>
        <kbd className="ml-auto rounded border border-slate-700 bg-slate-800 px-1.5 py-0.5 font-mono text-[10px] text-slate-400">
          F12
        </kbd>
      </button>
      {open && (
        <div className="flex h-56 flex-col border-t border-slate-800">
          <div
            ref={outputRef}
            className="min-h-0 flex-1 overflow-y-auto px-3 py-2 font-mono"
          >
            {entries.length === 0 && (
              <p className="text-slate-500">
                Code runs in the page shown above, with the page's own scripts
                in scope.
              </p>
            )}
            {entries.map((entry) => (
              <div key={entry.id} className="border-b border-slate-800 py-1">
                <pre className="whitespace-pre-wrap break-all text-sky-300">
                  {`> ${entry.expression}`}
                </pre>
                {entry.result === null ? (
                  <p className="text-slate-500">Running…</p>
                ) : (
                  <>
                    <pre
                      className={cn(
                        "whitespace-pre-wrap break-all",
                        STATUS_CLASS[entry.result.status] ?? "text-slate-100",
                      )}
                    >
                      {entry.result.text}
                    </pre>
                    {entry.result.url && (
                      <p className="truncate text-slate-500">
                        {entry.result.url}
                      </p>
                    )}
                  </>
                )}
              </div>
            ))}
          </div>
          <div className="flex items-start gap-2 border-t border-slate-800 px-3 py-2">
            <span className="pt-1 font-mono text-sky-300">&gt;</span>
            <textarea
              ref={textareaRef}
              value={value}
              onChange={(e) => {
                historyIndexRef.current = null;
                setValue(e.target.value);
              }}
              onKeyDown={handleKeyDown}
              rows={2}
              disabled={!canRun}
              spellCheck={false}
              aria-label="JavaScript to run in the page"
              placeholder={
                canRun
                  ? "Type JavaScript. Enter runs it, Shift+Enter adds a line."
                  : "Take control of the browser to run code."
              }
              className="min-h-[2.5rem] flex-1 resize-none bg-transparent font-mono text-slate-100 outline-none placeholder:text-slate-500 disabled:cursor-not-allowed"
            />
            <Button
              size="sm"
              variant="secondary"
              disabled={!canRun || running || !value.trim()}
              onClick={() => void run()}
            >
              Run
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={entries.length === 0}
              onClick={() => setEntries([])}
            >
              Clear
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

export { BrowserSessionConsole };
