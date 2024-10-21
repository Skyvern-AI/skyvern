import CodeMirror, { EditorView } from "@uiw/react-codemirror";
import { json } from "@codemirror/lang-json";
import { python } from "@codemirror/lang-python";
import { tokyoNightStorm } from "@uiw/codemirror-theme-tokyo-night-storm";
import { cn } from "@/util/utils";

type Props = {
  value: string;
  onChange?: (value: string) => void;
  language: "python" | "json";
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
  className,
  readOnly = false,
  fontSize = 12,
}: Props) {
  const extensions =
    language === "json"
      ? [json(), EditorView.lineWrapping]
      : [python(), EditorView.lineWrapping];
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
