import { create } from "zustand";

type AutoplayStore = {
  wpid: string | null;
  blockLabel: string | null;
  setAutoplay: (wpid: string | null, blockLabel: string | null) => void;
  clearAutoplay: () => void;
  getAutoplay: () => { wpid: string | null; blockLabel: string | null };
};

export const useAutoplayStore = create<AutoplayStore>((set, get) => ({
  wpid: null,
  blockLabel: null,
  setAutoplay: (wpid: string | null, blockLabel: string | null) => {
    set({ wpid, blockLabel });
  },
  clearAutoplay: () => {
    set({ wpid: null, blockLabel: null });
  },
  getAutoplay: () => {
    const { wpid, blockLabel } = get();
    return { wpid, blockLabel };
  },
}));
