import { FileIcon, UploadIcon } from "@radix-ui/react-icons";
import { useRef } from "react";

import { Button } from "@/components/ui/button";

import {
  AttentionMarker,
  AttentionTray,
  type AttentionTrayPresentation,
} from "./AttentionTray";

// The page opened a file chooser during a demonstration; the remote browser's own dialog is unusable.
export function DemonstrationFileTray({
  label,
  busy,
  onChoose,
  tray,
}: {
  label: string;
  busy: boolean;
  onChoose: (file: File | null) => void;
  tray: AttentionTrayPresentation;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const title = `Choose a file for “${label}”`;
  return (
    <AttentionTray
      aria-label="File request"
      title={title}
      wrapTitle
      collapsedTitle="The page wants a file"
      collapsed={tray.collapsed}
      onCollapsedChange={tray.onCollapsedChange}
      minimizeLabel="Minimize file request"
      upNext={tray.upNext}
      embedded={tray.embedded}
    >
      <div className="px-3 pb-2.5 pt-1 text-[11px] leading-relaxed text-muted-foreground">
        The page opened a file picker. Choose the file to attach here; Copilot
        saves it as the default for the workflow&apos;s file input.
      </div>
      <div className="flex shrink-0 flex-wrap items-center gap-2 border-t border-border px-3 py-1.5">
        <Button
          type="button"
          size="sm"
          disabled={busy}
          onClick={() => inputRef.current?.click()}
        >
          <UploadIcon className="mr-1.5 size-3.5" />
          Choose file
        </Button>
        <input
          ref={inputRef}
          type="file"
          className="sr-only"
          tabIndex={-1}
          aria-hidden
          onChange={(event) => {
            const file = event.target.files?.[0];
            event.target.value = "";
            if (file) onChoose(file);
          }}
        />
        <div className="ml-auto">
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={busy}
            onClick={() => onChoose(null)}
          >
            Skip
          </Button>
        </div>
      </div>
    </AttentionTray>
  );
}

// Where the file request was raised in the transcript; the tray holds the actions.
export function DemonstrationFileMarker() {
  return (
    <AttentionMarker
      icon={<FileIcon aria-hidden className="size-3.5 shrink-0" />}
      title="The page wants a file"
    />
  );
}
