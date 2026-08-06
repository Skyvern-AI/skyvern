import { create } from "zustand";

const DEFAULT_WORKFLOW_TITLE = "New Agent" as const;
// "New Workflow" is the backend's placeholder; both mean "nobody has named this yet".
const DEFAULT_WORKFLOW_TITLES: readonly string[] = [
  DEFAULT_WORKFLOW_TITLE,
  "New Workflow",
];

// "" is the pre-hydration state, before initializeTitle runs — an unnamed agent for
// every purpose here, so a title arriving mid-hydration is applied rather than dropped.
const isDefaultTitle = (title: string) =>
  title.trim() === "" || DEFAULT_WORKFLOW_TITLES.includes(title.trim());

type WorkflowTitleStore = {
  title: string;
  titleHasBeenGenerated: boolean;
  isNewTitle: () => boolean;
  setTitle: (title: string) => void;
  setTitleFromGeneration: (title: string) => void;
  setTitleFromCopilotIfDefault: (title: string) => void;
  syncTitleFromWorkflow: (title: string) => void;
  initializeTitle: (title: string) => void;
  resetTitle: () => void;
};

const useWorkflowTitleStore = create<WorkflowTitleStore>((set, get) => {
  return {
    title: "",
    titleHasBeenGenerated: false,
    isNewTitle: () => {
      return isDefaultTitle(get().title);
    },
    setTitle: (title: string) => {
      set({ title: title.trim(), titleHasBeenGenerated: true });
    },
    // Automatic canvas syncs (canonical load, snap-back) replay a snapshot that can
    // carry the placeholder the agent has since been named past. Nothing automatic may
    // un-name an agent; only the user's own rename can.
    syncTitleFromWorkflow: (title: string) => {
      if (isDefaultTitle(title) && !isDefaultTitle(get().title)) {
        return;
      }
      set({
        title: title.trim(),
        titleHasBeenGenerated: !isDefaultTitle(title),
      });
    },
    setTitleFromGeneration: (title: string) => {
      set({ title: title.trim(), titleHasBeenGenerated: true });
    },
    initializeTitle: (title: string) => {
      set({
        title: title.trim(),
        titleHasBeenGenerated: !isDefaultTitle(title),
      });
    },
    // A Copilot push must not overwrite a name the user chose, and a push that still
    // carries a placeholder must not count as "generated" — that flag permanently
    // disarms the auto-titler.
    setTitleFromCopilotIfDefault: (title: string) => {
      if (!isDefaultTitle(get().title)) {
        return;
      }
      set({
        title: title.trim(),
        titleHasBeenGenerated: !isDefaultTitle(title),
      });
    },
    resetTitle: () => {
      set({ title: "", titleHasBeenGenerated: false });
    },
  };
});

export { useWorkflowTitleStore };
