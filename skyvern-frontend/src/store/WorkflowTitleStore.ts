/**
 * Context: new workflows begin with a default title. If the user edits a URL
 * field in a workflow block, and the title is deemed "new", we want to
 * automagically update the title to the text of the URL. That way, they don't
 * have to manually update the title themselves, if they deem the automagic
 * title to be appropriate.
 */
import { create } from "zustand";

const DEFAULT_WORKFLOW_TITLE = "New Workflow" as const;
const DELIMITER_OPEN = "[[";
const DELIMITER_CLOSE = "]]";

type WorkflowTitleStore = {
  title: string;
  /**
   * If the title is deemed to be new, accept it, and prevent further
   * `maybeWriteTitle` updates.
   */
  maybeAcceptTitle: () => void;
  /**
   * Maybe update the title - if it's empty, or deemed to be new and unedited.
   */
  maybeWriteTitle: (title: string) => void;
  setTitle: (title: string) => void;
  initializeTitle: (title: string) => void;
  resetTitle: () => void;
};
/**
 * If the title appears to be a URL, let's trim it down to the domain and path.
 */
const formatURL = (url: string) => {
  try {
    const urlObj = new URL(url);
    return urlObj.hostname + urlObj.pathname;
  } catch {
    return url;
  }
};

/**
 * If the title begins and ends with squackets, remove them.
 */
const formatAcceptedTitle = (title: string) => {
  if (title.startsWith(DELIMITER_OPEN) && title.endsWith(DELIMITER_CLOSE)) {
    const trimmed = title.slice(DELIMITER_OPEN.length, -DELIMITER_CLOSE.length);

    return formatURL(trimmed);
  }

  return title;
};

const formatNewTitle = (title: string) =>
  title.trim().length
    ? `${DELIMITER_OPEN}${title}${DELIMITER_CLOSE}`
    : DEFAULT_WORKFLOW_TITLE;

const isNewTitle = (title: string) =>
  title === DEFAULT_WORKFLOW_TITLE ||
  (title.startsWith(DELIMITER_OPEN) && title.endsWith(DELIMITER_CLOSE));

const useWorkflowTitleStore = create<WorkflowTitleStore>((set, get) => {
  return {
    title: "",
    maybeAcceptTitle: () => {
      const { title: currentTitle } = get();
      if (isNewTitle(currentTitle)) {
        set({ title: formatAcceptedTitle(currentTitle) });
      }
    },
    maybeWriteTitle: (title: string) => {
      const { title: currentTitle } = get();
      if (isNewTitle(currentTitle)) {
        set({ title: formatNewTitle(title.trim()) });
      }
    },
    setTitle: (title: string) => {
      set({ title: title.trim() });
    },
    initializeTitle: (title: string) => {
      set({ title: title.trim() });
    },
    resetTitle: () => {
      set({ title: "" });
    },
  };
});

export { useWorkflowTitleStore };
