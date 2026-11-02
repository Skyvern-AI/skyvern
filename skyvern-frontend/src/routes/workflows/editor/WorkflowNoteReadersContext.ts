import { createContext } from "react";

export const WorkflowNoteReadersContext = createContext<
  ReadonlyMap<string, Array<string>>
>(new Map());
