import { create } from "zustand";

const DEFAULT_WORKFLOW_TITLE = "New Agent" as const;

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
      const normalizedTitle = (title ?? "").trim();
      set({ title: normalizedTitle, titleHasBeenGenerated: true });
    },
    setTitleFromGeneration: (title: string) => {
      const normalizedTitle = (title ?? "").trim();
      set({ title: normalizedTitle, titleHasBeenGenerated: true });
    },
    initializeTitle: (title: string) => {
      const normalizedTitle = (title ?? "").trim();
      set({
        title: normalizedTitle,
        titleHasBeenGenerated: normalizedTitle !== DEFAULT_WORKFLOW_TITLE,
      });
    },
    resetTitle: () => {
      set({ title: "", titleHasBeenGenerated: false });
    },
  };
});

export { useWorkflowTitleStore };
