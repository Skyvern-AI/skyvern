import { useEffect, useRef } from "react";
import { EditorView, lineNumbers } from "@codemirror/view";
import { EditorState } from "@codemirror/state";
import { python } from "@codemirror/lang-python";
import { unifiedMergeView } from "@codemirror/merge";
import { tokyoNightStorm } from "@uiw/codemirror-theme-tokyo-night-storm";
import { tokyoNightDay } from "@uiw/codemirror-theme-tokyo-night-day";
import { useThemeAsDarkOrLight } from "@/components/useThemeAsDarkOrLight";

function ScriptDiffViewer({
  original,
  modified,
}: {
  original: string;
  modified: string;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView | null>(null);
  const themeMode = useThemeAsDarkOrLight();

  useEffect(() => {
    if (!containerRef.current) return;

    // Clean up previous editor
    if (viewRef.current) {
      viewRef.current.destroy();
      viewRef.current = null;
    }

    const view = new EditorView({
      state: EditorState.create({
        doc: modified,
        extensions: [
          EditorView.editable.of(false),
          EditorState.readOnly.of(true),
          lineNumbers(),
          python(),
          themeMode === "dark" ? tokyoNightStorm : tokyoNightDay,
          unifiedMergeView({
            original,
            highlightChanges: true,
            gutter: true,
            syntaxHighlightDeletions: true,
          }),
          EditorView.theme({
            "&": { maxHeight: "400px", fontSize: "11px" },
            ".cm-scroller": { overflow: "auto" },
          }),
        ],
      }),
      parent: containerRef.current,
    });

    viewRef.current = view;

    return () => {
      view.destroy();
      viewRef.current = null;
    };
  }, [original, modified, themeMode]);

  return <div ref={containerRef} />;
}

export { ScriptDiffViewer };
