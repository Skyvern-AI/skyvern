import { create } from "zustand";

type ShowAllCodeStore = {
  showAllCode: boolean;
  setShowAllCode: (value: boolean) => void;
  toggleShowAllCode: () => void;
  reset: () => void;
};

const useShowAllCodeStore = create<ShowAllCodeStore>((set, get) => {
  return {
    showAllCode: false,
    setShowAllCode: (value: boolean) => {
      set({ showAllCode: value });
    },
    toggleShowAllCode: () => {
      set({ showAllCode: !get().showAllCode });
    },
    reset: () => {
      set({ showAllCode: false });
    },
  };
});

export { useShowAllCodeStore };
