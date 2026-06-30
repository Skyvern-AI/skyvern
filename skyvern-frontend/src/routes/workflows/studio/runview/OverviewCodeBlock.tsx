import { CopyButton } from "@/components/CopyButton";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";

export function OverviewCodeBlock({
  value,
  maxHeight = "220px",
}: {
  value: string;
  maxHeight?: string;
}) {
  return (
    <div className="relative">
      <div className="absolute right-2 top-2 z-10">
        <CopyButton
          value={value}
          className="h-7 w-7 bg-slate-elevation3/80 text-muted-foreground backdrop-blur hover:bg-slate-elevation4 hover:text-foreground"
        />
      </div>
      <CodeEditor
        language="json"
        value={value}
        readOnly
        maxHeight={maxHeight}
      />
    </div>
  );
}
