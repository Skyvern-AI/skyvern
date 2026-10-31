import type { ReactNode } from "react";

import { WorkflowNoteReadersContext } from "./WorkflowNoteReadersContext";

export function WorkflowNoteReadersProvider({
  readers,
  children,
}: {
  readers: ReadonlyMap<string, Array<string>>;
  children: ReactNode;
}) {
  return (
    <WorkflowNoteReadersContext.Provider value={readers}>
      {children}
    </WorkflowNoteReadersContext.Provider>
  );
}
