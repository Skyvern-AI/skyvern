import { create } from "zustand";

export type DocRunCheckProblem = {
  key: string;
  step: string | null;
  message: string;
};

const NONE: DocRunCheckProblem[] = [];

// Only a mounted Doc fills this, so the Visual editor's Run never asks.
export const useDocRunCheckStore = create<{
  critical: DocRunCheckProblem[];
}>(() => ({ critical: NONE }));

export function publishDocCriticalProblems(problems: DocRunCheckProblem[]) {
  useDocRunCheckStore.setState({
    critical: problems.length ? problems : NONE,
  });
}
