import CodeMirror, { EditorView } from "@uiw/react-codemirror";
import { json } from "@codemirror/lang-json";
import { python } from "@codemirror/lang-python";
import { html } from "@codemirror/lang-html";
import { tokyoNightStorm } from "@uiw/codemirror-theme-tokyo-night-storm";
import { useEffect, useRef, useState } from "react";
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
};

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
}: Props) {
  const viewRef = useRef<EditorView | null>(null);
  const [internalValue, setInternalValue] = useState(value);

  useEffect(() => {
    setInternalValue(value);
  }, [value]);

  const debouncedOnChange = useDebouncedCallback((newValue: string) => {
    onChange?.(newValue);
  }, 300);

  const handleChange = (newValue: string) => {
    setInternalValue(newValue);
    debouncedOnChange(newValue);
  };

  const extensions = language
    ? [getLanguageExtension(language), lineWrap ? EditorView.lineWrapping : []]
    : [lineWrap ? EditorView.lineWrapping : []];

  const style: React.CSSProperties = { fontSize };
  if (fullHeight) {
    extensions.push(fullHeightExtension);
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
      onCreateEditor={(view) => {
        viewRef.current = view;
      }}
      onUpdate={(viewUpdate) => {
        if (!viewRef.current) viewRef.current = viewUpdate.view;
      }}
      onBlur={() => {
        debouncedOnChange.flush();
      }}
    />
  );
}

export { CodeEditor };
