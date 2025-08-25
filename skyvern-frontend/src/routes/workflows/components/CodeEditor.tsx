import CodeMirror, { EditorView } from "@uiw/react-codemirror";
import { json } from "@codemirror/lang-json";
import { python } from "@codemirror/lang-python";
import { html } from "@codemirror/lang-html";
import { tokyoNightStorm } from "@uiw/codemirror-theme-tokyo-night-storm";
import { cn } from "@/util/utils";

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
};

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
}: Props) {
  const extensions = language
    ? [getLanguageExtension(language), lineWrap ? EditorView.lineWrapping : []]
    : [lineWrap ? EditorView.lineWrapping : []];

  return (
    <CodeMirror
      value={value}
      onChange={onChange}
      extensions={extensions}
      theme={tokyoNightStorm}
      minHeight={minHeight}
      maxHeight={maxHeight}
      readOnly={readOnly}
      className={cn("cursor-auto", className)}
      style={{
        fontSize: fontSize,
      }}
    />
  );
}

export { CodeEditor };
