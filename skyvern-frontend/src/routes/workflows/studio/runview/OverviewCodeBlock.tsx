import { useRef } from "react";
import { MagnifyingGlassIcon } from "@radix-ui/react-icons";
import { EditorView, keymap } from "@uiw/react-codemirror";
import { openSearchPanel, search, searchKeymap } from "@codemirror/search";

import { CopyButton } from "@/components/CopyButton";
import { Button } from "@/components/ui/button";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";

const SEARCH_EXTENSIONS = [search(), keymap.of(searchKeymap)];

export function OverviewCodeBlock({
  value,
  maxHeight = "220px",
}: {
  value: string;
  maxHeight?: string;
}) {
  const viewRef = useRef<EditorView | null>(null);

  return (
    <div className="relative">
      <div className="absolute right-2 top-2 z-10 flex gap-1">
        <Button
          size="icon"
          variant="ghost"
          aria-label="Search"
          className="h-7 w-7 bg-slate-elevation3/80 text-muted-foreground backdrop-blur hover:bg-slate-elevation4 hover:text-foreground"
          onClick={() => {
            const view = viewRef.current;
            if (view) {
              openSearchPanel(view);
            }
          }}
        >
          <MagnifyingGlassIcon />
        </Button>
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
        extraExtensions={SEARCH_EXTENSIONS}
        onEditorView={(view) => {
          viewRef.current = view;
        }}
      />
    </div>
  );
}
