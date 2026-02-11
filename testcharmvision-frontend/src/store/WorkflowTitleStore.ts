import { create } from "zustand";

const DEFAULT_WORKFLOW_TITLE = "New Workflow" as const;

type WorkflowTitleStore = {
  title: string;
  titleHasBeenGenerated: boolean;
  isNewTitle: () => boolean;
  setTitle: (title: string) => void;
  setTitleFromGeneration: (title: string) => void;
  initializeTitle: (title: string) => void;
  resetTitle: () => void;
};

const useWorkflowTitleStore = create<WorkflowTitleStore>((set, get) => {
  return {
    title: "",
    titleHasBeenGenerated: false,
    isNewTitle: () => {
      return get().title === DEFAULT_WORKFLOW_TITLE;
    },
    setTitle: (title: string) => {
      set({ title: title.trim(), titleHasBeenGenerated: true });
    },
    setTitleFromGeneration: (title: string) => {
      set({ title: title.trim(), titleHasBeenGenerated: true });
    },
    initializeTitle: (title: string) => {
      set({
        title: title.trim(),
        titleHasBeenGenerated: title.trim() !== DEFAULT_WORKFLOW_TITLE,
      });
    },
    resetTitle: () => {
      set({ title: "", titleHasBeenGenerated: false });
    },
  };
});

export { useWorkflowTitleStore };
