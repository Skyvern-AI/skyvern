import { FileTextIcon } from "@radix-ui/react-icons";
import { useContext, type ComponentType } from "react";
import type { NodeProps } from "@xyflow/react";

import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { WorkflowNoteReadersContext } from "../WorkflowNoteReadersContext";

export function withNoteReadMarker<P extends NodeProps>(
  Component: ComponentType<P>,
): ComponentType<P> {
  function NoteReadMarker(props: P) {
    const noteReaderTexts = useContext(WorkflowNoteReadersContext).get(
      props.id,
    );

    // The node keeps the same element position whether or not it has notes, so adding the first note
    // never remounts it. The marker is positioned by React Flow's absolutely positioned node wrapper.
    return (
      <>
        <Component {...props} />
        {noteReaderTexts?.length ? (
          <TooltipProvider delayDuration={150}>
            <Tooltip>
              <TooltipTrigger asChild>
                <button
                  type="button"
                  aria-label={`This step has ${noteReaderTexts.length} note${
                    noteReaderTexts.length === 1 ? "" : "s"
                  }`}
                  className="absolute right-2 top-2 z-10 rounded-sm bg-violet-100 p-1 text-violet-700 shadow-sm ring-1 ring-violet-300 hover:bg-violet-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-500 dark:bg-violet-950 dark:text-violet-200 dark:ring-violet-700 dark:hover:bg-violet-900"
                  onClick={(event) => event.stopPropagation()}
                  onPointerDown={(event) => event.stopPropagation()}
                >
                  <FileTextIcon aria-hidden="true" className="h-3 w-3" />
                </button>
              </TooltipTrigger>
              <TooltipContent
                side="bottom"
                collisionBoundary={document.documentElement}
                collisionPadding={8}
                className="max-w-[min(24rem,calc(100vw-16px))] whitespace-normal [overflow-wrap:anywhere]"
              >
                <p className="mb-1 font-medium">Notes on this step</p>
                {noteReaderTexts.map((text, index) => (
                  <p key={`${index}-${text}`} className="whitespace-pre-wrap">
                    {text}
                  </p>
                ))}
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
        ) : null}
      </>
    );
  }

  NoteReadMarker.displayName = `withNoteReadMarker(${Component.displayName ?? Component.name ?? "Component"})`;
  return NoteReadMarker;
}
