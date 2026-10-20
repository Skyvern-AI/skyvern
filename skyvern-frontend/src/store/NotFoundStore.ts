import { create } from "zustand";

type NotFoundStore = {
  // A count rather than a flag: route transitions can mount the next not-found
  // screen before the previous one unmounts, and StrictMode double-invokes the
  // registering effect.
  visibleCount: number;
  showNotFound: () => void;
  hideNotFound: () => void;
};

const useNotFoundStore = create<NotFoundStore>((set) => ({
  visibleCount: 0,
  showNotFound: () =>
    set((state) => ({ visibleCount: state.visibleCount + 1 })),
  hideNotFound: () =>
    set((state) => ({ visibleCount: Math.max(0, state.visibleCount - 1) })),
}));

function useNotFoundVisible(): boolean {
  return useNotFoundStore((state) => state.visibleCount > 0);
}

export { useNotFoundStore, useNotFoundVisible };
