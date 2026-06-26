import CodeMirror, { EditorView, type Extension } from "@uiw/react-codemirror";
import type { ViewUpdate } from "@codemirror/view";
import { json } from "@codemirror/lang-json";
import { python } from "@codemirror/lang-python";
import { html } from "@codemirror/lang-html";
import { tokyoNightStorm } from "@uiw/codemirror-theme-tokyo-night-storm";
import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { cn } from "@/util/utils";
import { useDebouncedCallback } from "use-debounce";

import { isOversizedDocument } from "./oversizedDocument";
import "./code-mirror-overrides.css";

function getLanguageExtension(language: "python" | "json" | "html") {
  switch (language) {
    case "python":
      return python();
    case "json":
      return json();
    case "html":
      return html();
  }
}

type Props = {
  value: string;
  onChange?: (value: string) => void;
  language?: "python" | "json" | "html";
  lineWrap?: boolean;
  readOnly?: boolean;
  minHeight?: string;
  maxHeight?: string;
  className?: string;
  fontSize?: number;
  fullHeight?: boolean;
  /**
   * Additional CodeMirror extensions. Useful for per-use-case concerns
   * like linting — e.g. the error_code_mapping editor passes a linter
   * that flags whitespace-bearing keys inline on the offending line.
   * Pass a stable (e.g. module-level) reference to avoid editor churn.
   */
  extraExtensions?: Extension[];
} & Pick<React.HTMLAttributes<HTMLDivElement>, "aria-required">;

const fullHeightExtension = EditorView.theme({
  "&": { height: "100%" },
  ".cm-scroller": { flex: 1 },
});

// Pre-mount margin: render the editor while it is still ~200 px outside the
// viewport so it is ready before the user scrolls/pans to it. Trades a small
// over-mount budget for no visible empty-placeholder flash.
const VIEWPORT_PREMOUNT_MARGIN = "200px";

function CodeEditorImpl({
  value,
  onChange,
  minHeight,
  maxHeight,
  language,
  lineWrap = true,
  className,
  readOnly = false,
  fontSize = 12,
  fullHeight = false,
  extraExtensions,
  ...restProps
}: Props) {
  const viewRef = useRef<EditorView | null>(null);
  const [internalValue, setInternalValue] = useState(value);
  const latestValueRef = useRef(value);

  // Defer EditorView creation until the container is in (or near) the
  // viewport. Block editors mount many CodeEditors at once (script-mode
  // toggle, accordion content), and CodeMirror's useLayoutEffect-driven
  // EditorView constructor is heavy enough to dominate a React commit with
  // multiple instances — see the trace at SKY-9051 showing ~1.3 s of style
  // recalc per interaction with off-screen editors in scope. Once visible,
  // stay mounted so panning back and forth doesn't tear down editor state.
  const placeholderRef = useRef<HTMLDivElement>(null);
  const [shouldMount, setShouldMount] = useState<boolean>(
    typeof IntersectionObserver === "undefined",
  );

  useEffect(() => {
    if (shouldMount) return;
    const el = placeholderRef.current;
    if (!el) {
      setShouldMount(true);
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setShouldMount(true);
          observer.disconnect();
        }
      },
      { rootMargin: VIEWPORT_PREMOUNT_MARGIN },
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [shouldMount]);

  useEffect(() => {
    setInternalValue(value);
    latestValueRef.current = value;
  }, [value]);

  // Capture the latest onChange in a ref so the debounced callback below
  // (and the React.memo wrapper export) stay referentially stable across
  // re-renders. Without this, an inline `onChange={...}` prop from a
  // re-rendering parent would invalidate memoization and keep CodeMirror
  // re-dispatching even when the editor's own state hasn't changed —
  // amplifying axe-core measurement passes into a multi-second freeze (see
  // SKY-9051 trace: ~25k readSelectionRange samples in a 3.9 s freeze).
  const latestOnChangeRef = useRef(onChange);
  useEffect(() => {
    latestOnChangeRef.current = onChange;
  }, [onChange]);

  const debouncedOnChange = useDebouncedCallback((newValue: string) => {
    latestOnChangeRef.current?.(newValue);
  }, 300);

  const handleChange = useCallback(
    (newValue: string) => {
      setInternalValue(newValue);
      latestValueRef.current = newValue;
      debouncedOnChange(newValue);
    },
    [debouncedOnChange],
  );

  const handleBlur = useCallback(() => {
    debouncedOnChange.cancel();
    latestOnChangeRef.current?.(latestValueRef.current);
  }, [debouncedOnChange]);

  const handleCreateEditor = useCallback((view: EditorView) => {
    viewRef.current = view;
  }, []);

  const handleEditorUpdate = useCallback((viewUpdate: ViewUpdate) => {
    if (!viewRef.current) viewRef.current = viewUpdate.view;
  }, []);

  const oversized = useMemo(
    () => isOversizedDocument(internalValue),
    [internalValue],
  );
  const effectiveLineWrap = lineWrap && !oversized;

  // Memoize the extension tuple so React hands CodeMirror a stable
  // reference across renders. Without this, a parent re-render would
  // rebuild the array (and anything spread in) every cycle and trigger
  // unnecessary editor state reconfiguration.
  const extensions = useMemo<Extension[]>(() => {
    const exts: Extension[] = [];
    if (language && !oversized) {
      exts.push(getLanguageExtension(language));
    }
    if (effectiveLineWrap) {
      exts.push(EditorView.lineWrapping);
    }
    if (extraExtensions) {
      exts.push(...extraExtensions);
    }
    if (fullHeight) {
      exts.push(fullHeightExtension);
    }
    return exts;
  }, [language, oversized, effectiveLineWrap, extraExtensions, fullHeight]);

  const style: React.CSSProperties = { fontSize };
  if (fullHeight) {
    style.height = "100%";
  }

  useEffect(() => {
    const view = viewRef.current;
    if (!view) {
      return;
    }

    const el = view.scrollDOM; // this is the .cm-scroller element

    const onWheel = (e: WheelEvent) => {
      if (e.ctrlKey || e.metaKey) return;

      const factor =
        e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? el.clientHeight : 1;
      const dy = e.deltaY * factor;
      const dx = e.deltaX * factor;

      const top = el.scrollTop;
      const left = el.scrollLeft;
      const maxY = el.scrollHeight - el.clientHeight;
      const maxX = el.scrollWidth - el.clientWidth;

      const atTop = top <= 0;
      const atBottom = top >= maxY - 1;
      const atLeft = left <= 0;
      const atRight = left >= maxX - 1;

      const verticalWouldScroll = (dy < 0 && !atTop) || (dy > 0 && !atBottom);
      const horizontalWouldScroll = (dx < 0 && !atLeft) || (dx > 0 && !atRight);

      if (verticalWouldScroll || horizontalWouldScroll) {
        e.stopPropagation();
      }
    };

    el.addEventListener("wheel", onWheel, { passive: true, capture: true });

    return () => el.removeEventListener("wheel", onWheel, { capture: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [viewRef.current]);

  if (!shouldMount) {
    // The placeholder reserves the editor's footprint so the IntersectionObserver
    // hit-tests against a real layout box and the mount swap is visually quiet.
    const placeholderStyle: React.CSSProperties = { ...style };
    if (minHeight) placeholderStyle.minHeight = minHeight;
    if (maxHeight) placeholderStyle.maxHeight = maxHeight;
    return (
      <div
        ref={placeholderRef}
        className={cn("cursor-auto", className)}
        style={placeholderStyle}
        data-codeeditor-state="pending"
      />
    );
  }

  return (
    <CodeMirror
      value={internalValue}
      onChange={handleChange}
      extensions={extensions}
      theme={tokyoNightStorm}
      minHeight={minHeight}
      maxHeight={maxHeight}
      readOnly={readOnly}
      className={cn("cursor-auto", className)}
      style={style}
      {...restProps}
      onCreateEditor={handleCreateEditor}
      onUpdate={handleEditorUpdate}
      onBlur={handleBlur}
    />
  );
}

// React.memo: parents that pass a stable `extraExtensions` (per docs) and
// otherwise primitive props now get cheap re-renders. `onChange` is captured
// via ref so inline callbacks don't invalidate the memo. The CodeMirror
// dispatch cycle is expensive enough under accessibility-extension load
// (axe DevTools / Lighthouse) that even one skipped render per parent
// commit is meaningful.
const CodeEditor = memo(CodeEditorImpl);

export { CodeEditor };
