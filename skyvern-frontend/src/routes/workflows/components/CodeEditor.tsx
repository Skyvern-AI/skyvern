import CodeMirror, { EditorView, type Extension } from "@uiw/react-codemirror";
import { json } from "@codemirror/lang-json";
import { python } from "@codemirror/lang-python";
import { html } from "@codemirror/lang-html";
import { tokyoNightStorm } from "@uiw/codemirror-theme-tokyo-night-storm";
import { useEffect, useMemo, useRef, useState } from "react";
import { cn } from "@/util/utils";
import { useDebouncedCallback } from "use-debounce";

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

function CodeEditor({
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

  useEffect(() => {
    setInternalValue(value);
    latestValueRef.current = value;
  }, [value]);

  const debouncedOnChange = useDebouncedCallback((newValue: string) => {
    onChange?.(newValue);
  }, 300);

  const handleChange = (newValue: string) => {
    setInternalValue(newValue);
    latestValueRef.current = newValue;
    debouncedOnChange(newValue);
  };

  // Memoize the extension tuple so React hands CodeMirror a stable
  // reference across renders. Without this, a parent re-render would
  // rebuild the array (and anything spread in) every cycle and trigger
  // unnecessary editor state reconfiguration.
  const extensions = useMemo<Extension[]>(() => {
    const exts: Extension[] = language
      ? [
          getLanguageExtension(language),
          lineWrap ? EditorView.lineWrapping : [],
        ]
      : [lineWrap ? EditorView.lineWrapping : []];
    if (extraExtensions) {
      exts.push(...extraExtensions);
    }
    if (fullHeight) {
      exts.push(fullHeightExtension);
    }
    return exts;
  }, [language, lineWrap, extraExtensions, fullHeight]);

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
      onCreateEditor={(view) => {
        viewRef.current = view;
      }}
      onUpdate={(viewUpdate) => {
        if (!viewRef.current) viewRef.current = viewUpdate.view;
      }}
      onBlur={() => {
        debouncedOnChange.cancel();
        onChange?.(latestValueRef.current);
      }}
    />
  );
}

export { CodeEditor };
